"""Lectura de entorno validada al arrancar (12-factor).

Sin dependencias externas: si falta un valor crítico, el proceso no arranca
(`ImproperlyConfigured`) en lugar de usar un valor por defecto inseguro.
"""

import base64
import os
import re
from typing import Any
from urllib.parse import unquote, urlsplit

from django.core.exceptions import ImproperlyConfigured

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}
# Esquema, host (un nombre en ASCII o una IPv6 entre corchetes) y puerto. Nada más: un espacio,
# una barra invertida o un carácter de control los lee cada navegador a su manera.
_ORIGIN = re.compile(
    r"https?://([A-Za-z0-9][A-Za-z0-9._-]{0,252}|\[[0-9A-Fa-f:.]+\])(:[0-9]{1,5})?"
)
# `versión:clave`. 43 caracteres de base64 para URL son 32 bytes; el relleno `=` es opcional.
_KEY = re.compile(r"([1-9][0-9]{0,4}):([A-Za-z0-9_-]{43})=?")


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ImproperlyConfigured(f"Falta la variable de entorno obligatoria {name}")
    return value


def optional(name: str, default: str) -> str:
    return os.environ.get(name, default).strip()


def boolean(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ImproperlyConfigured(f"{name} debe ser booleano, no {raw!r}")


def integer(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip()
    if not (value.isascii() and value.isdigit() and len(value) < 10):
        raise ImproperlyConfigured(f"{name} debe ser un entero no negativo, no {raw!r}")
    return int(value)


def keys(name: str) -> dict[int, bytes]:
    """Claves de 256 bits con su versión (`1:<clave>,2:<clave>`; cada clave, 32 bytes en
    base64 para URL), o vacío si no se da. Las versiones van de 1 a 65535 y no se repiten;
    tampoco una clave. El error solo nombra la variable: su valor es un secreto."""
    value = os.environ.get(name, "").strip()
    found: dict[int, bytes] = {}
    for item in value.split(",") if value else []:
        match = _KEY.fullmatch(item.strip())
        version = int(match[1]) if match else 0
        key = base64.urlsafe_b64decode(match[2] + "=") if match else b""
        if not 0 < version < 2**16 or version in found or key in found.values() or len(key) != 32:
            raise ImproperlyConfigured(
                f"{name} debe ser una lista de `versión:clave`, con claves de 32 bytes en base64"
            )
        found[version] = key
    return found


def origin(name: str) -> str:
    """Un origen web (`https://app.example.com`, con puerto si lo lleva), o vacío si no se da.
    Sin ruta, consulta, fragmento ni credenciales: con él se componen enlaces."""
    value = os.environ.get(name, "").strip()
    if not value:
        return ""
    try:
        valid = bool(_ORIGIN.fullmatch(value)) and urlsplit(value).port != 0
    except ValueError:  # un puerto fuera de rango, o una dirección entre corchetes que no es IPv6
        valid = False
    if not valid:  # solo se nombra la variable: el valor puede llevar credenciales
        raise ImproperlyConfigured(f"{name} debe ser un origen http(s) sin ruta ni credenciales")
    return value


def csv_list(name: str) -> list[str]:
    return [item.strip() for item in os.environ.get(name, "").split(",") if item.strip()]


def database(url: str) -> dict[str, Any]:
    """Convierte postgres://user:pass@host:port/name en la configuración de Django."""
    parts = urlsplit(url)
    if parts.scheme not in ("postgres", "postgresql") or not parts.path.lstrip("/"):
        raise ImproperlyConfigured(
            "DATABASE_URL debe tener la forma postgres://user:pass@host:port/db"
        )
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": unquote(parts.path.lstrip("/")),
        "USER": unquote(parts.username or ""),
        "PASSWORD": unquote(parts.password or ""),
        "HOST": parts.hostname or "",
        "PORT": str(parts.port or ""),
        "CONN_MAX_AGE": 60,
        "CONN_HEALTH_CHECKS": True,
    }
