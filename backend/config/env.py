"""Lectura de entorno validada al arrancar (12-factor).

Sin dependencias externas: si falta un valor crítico, el proceso no arranca
(`ImproperlyConfigured`) en lugar de usar un valor por defecto inseguro.
"""

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
