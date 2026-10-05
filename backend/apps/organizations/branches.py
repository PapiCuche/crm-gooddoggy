"""Comandos de sucursales (F2-44 y F2-45, E01-09): crear una y cambiar sus datos.

No comprueban permisos: `organizations` no importa `access`. Por eso no son API pública: solo
los importa la API del módulo, que declara `branches.manage` (contrato de import-linter).
Validan lo que guardan, también para quien no llega por HTTP, y auditan cada cambio.
"""

import re
import zoneinfo
from collections.abc import Callable
from functools import cache, partial
from typing import Any
from uuid import UUID

from django.db import IntegrityError, transaction

from apps.audit.services import Entity, record
from apps.organizations.models import Branch
from apps.organizations.text import line, name
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


def branch_code(value: str) -> str:
    """El código como se guarda: en mayúsculas. Solo ASCII: `upper()` convertiría otras letras
    en letras ASCII (una «ı» sin punto en «I») y dos códigos distintos acabarían iguales."""
    code = value.strip().upper() if value.isascii() else ""
    if not (len(code) <= CODE_MAX and CODE.fullmatch(code)):
        raise ValueError(f"letras sin acento, cifras y guiones entre ellas, hasta {CODE_MAX}")
    return code


def branch_name(value: str) -> str:
    return name(value, NAME_MAX)


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
    **{field: partial(line, limit=limit) for field, limit in TEXT_MAX.items()},
    "timezone": branch_timezone,
}
CREATE = frozenset(CLEAN)
UPDATE = CREATE - {"code"} | {"is_active"}  # el código no cambia: así se nombra la sucursal


def cleaned(fields: dict[str, Any], allowed: frozenset[str] = CREATE) -> dict[str, Any]:
    """Cada campo como se guarda. `ValueError` con el nombre del primero que no sirve."""
    if unknown := set(fields) - allowed:
        raise ValueError(f"campos no admitidos: {sorted(unknown)}")
    result = {}
    for field, value in fields.items():
        try:
            if field == "is_active":
                if not isinstance(value, bool):
                    raise ValueError("tiene que ser verdadero o falso")
                result[field] = value
                continue
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


def update_branch(ctx: TenantContext, *, branch_id: UUID, **fields: Any) -> Branch:
    """Cambia lo que se envía de una sucursal de la organización de `ctx` y lo audita con el
    antes y el después. Si nada cambia, no escribe ni audita. Una sucursal de otra organización
    no existe: `DoesNotExist`."""
    alias = require_scope(ctx)
    values = cleaned(fields, UPDATE)
    with transaction.atomic(using=alias):  # savepoint: el cambio y su auditoría, o nada
        rows = Branch.objects.using(alias).select_for_update(no_key=True)
        branch: Branch = rows.get(pk=branch_id)
        changes = {
            field: [getattr(branch, field), value]
            for field, value in values.items()
            if getattr(branch, field) != value
        }
        if changes:
            for field, (_, value) in changes.items():
                setattr(branch, field, value)
            branch.save(using=alias, update_fields=[*changes, "updated_at"])
            record(ctx, "branch.updated", Entity("branch", branch.pk, branch.code), changes)
    return branch
