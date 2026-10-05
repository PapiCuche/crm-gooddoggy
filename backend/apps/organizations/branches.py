"""Comandos de sucursales (F2-44, E01-09): crear una.

No comprueban permisos: `organizations` no importa `access`. Por eso no son API pública: solo
los importa la API del módulo, que declara `branches.manage` (contrato de import-linter).
Validan lo que guardan, también para quien no llega por HTTP, y auditan.
"""

import re
import unicodedata
import zoneinfo
from collections.abc import Callable
from functools import cache, partial
from typing import Any

from django.db import IntegrityError, transaction

from apps.audit.services import Entity, record
from apps.organizations.models import Branch
from core.tenancy.context import TenantContext
from core.tenancy.scope import require_scope

CODE = re.compile(r"[A-Z0-9]+(-[A-Z0-9]+)*")  # la misma forma que impone la tabla
CODE_MAX, NAME_MAX, TIMEZONE_MAX = 20, 100, 64
TEXT_MAX = {"address": 255, "district": 100, "city": 100, "phone": 32}
DEFAULT_TIMEZONE = "America/Lima"
# Están entre las zonas y no lo son: «Factory» es la marca de «sin configurar» y `localtime`,
# el enlace de Debian a la hora del servidor.
NOT_ZONES = frozenset({"Factory", "localtime"})


class BranchCodeTaken(Exception):
    """Ya hay una sucursal con ese código en la organización."""


def _line(value: str, limit: int) -> str:
    """Una línea de texto como se guarda: forma NFC, sin espacios exteriores y con los
    interiores reducidos a uno. `ValueError` si lleva saltos de línea, controles o caracteres
    que no se ven, o si pasa de `limit`."""
    spaced = "".join(" " if unicodedata.category(char) == "Zs" else char for char in value)
    text = unicodedata.normalize("NFC", spaced)
    if not text.strip().isprintable():  # antes de limpiar: un salto de línea no es un espacio
        raise ValueError("solo texto imprimible, en una línea")
    text = " ".join(text.split())
    if len(text) > limit:
        raise ValueError(f"hasta {limit} caracteres")
    return text


def branch_code(value: str) -> str:
    """El código como se guarda: en mayúsculas. Solo ASCII: `upper()` convertiría otras letras
    en letras ASCII (una «ı» sin punto en «I») y dos códigos distintos acabarían iguales."""
    code = value.strip().upper() if value.isascii() else ""
    if not (len(code) <= CODE_MAX and CODE.fullmatch(code)):
        raise ValueError(f"letras sin acento, cifras y guiones entre ellas, hasta {CODE_MAX}")
    return code


def branch_name(value: str) -> str:
    name = _line(value, NAME_MAX)
    if not any(char.isalnum() for char in name):
        raise ValueError("nombre obligatorio, con alguna letra o cifra")
    return name


@cache
def _timezones() -> frozenset[str]:
    return frozenset(zoneinfo.available_timezones()) - NOT_ZONES


def branch_timezone(value: str) -> str:
    """Un nombre IANA que exista (`America/Lima`, `UTC`). Se busca en la lista: el texto del
    cliente nunca se usa para abrir un archivo."""
    if len(value) > TIMEZONE_MAX or value not in _timezones():
        raise ValueError("zona horaria IANA desconocida")
    return value


CLEAN: dict[str, Callable[[Any], Any]] = {
    "code": branch_code,
    "name": branch_name,
    **{field: partial(_line, limit=limit) for field, limit in TEXT_MAX.items()},
    "timezone": branch_timezone,
}


def cleaned(fields: dict[str, Any]) -> dict[str, Any]:
    """Cada campo como se guarda. `ValueError` con el nombre del primero que no sirve."""
    if unknown := set(fields) - set(CLEAN):
        raise ValueError(f"campos no admitidos: {sorted(unknown)}")
    result = {}
    for field, value in fields.items():
        try:
            if not isinstance(value, str):
                raise ValueError("tiene que ser texto")
            result[field] = CLEAN[field](value)
        except ValueError as error:
            raise ValueError(f"{field}: {error}") from None
    return result


def create_branch(ctx: TenantContext, **fields: Any) -> Branch:
    """Crea una sucursal activa en la organización de `ctx` y lo audita. `code` y `name` son
    obligatorios. Lanza `BranchCodeTaken` si el código ya existe, sin escribir nada."""
    alias = require_scope(ctx)
    if missing := {"code", "name"} - set(fields):
        raise ValueError(f"campos obligatorios: {sorted(missing)}")
    values = {"timezone": DEFAULT_TIMEZONE, **cleaned(fields)}
    try:
        with transaction.atomic(using=alias):  # savepoint: la sucursal y su auditoría, o nada
            branch: Branch = Branch.objects.using(alias).create(**values)
            changes = {field: [None, value] for field, value in values.items() if value != ""}
            record(ctx, "branch.created", Entity("branch", branch.pk, branch.code), changes)
    except IntegrityError as error:
        if "branches_org_code_uq" not in str(error):
            raise
        raise BranchCodeTaken(values["code"]) from None
    return branch
