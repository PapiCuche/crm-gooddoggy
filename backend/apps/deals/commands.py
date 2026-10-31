"""Comandos de motivos de pérdida (F5-01; E09-05, modelo de datos §E.6). Los de pipelines y etapas
llegan con F5-01b, en este mismo módulo.

No comprueban permisos: los declarará la API (`pipelines.manage`, F5-02), la única que los importa
(contrato de import-linter). Validan lo que guardan y auditan cada cambio (sin datos personales).
"""

import re
from collections.abc import Callable
from typing import Any
from uuid import UUID

from django.db import IntegrityError, transaction
from django.db.models import F, Max

from apps.audit.services import Entity, record
from apps.deals.models import REASON_CODE, LostReason
from core.tenancy.context import TenantContext
from core.tenancy.scope import require_scope
from core.text import name as clean_name

NAME_MAX, CODE_MAX = 100, 30
_REASON_FIELDS = frozenset({"name", "sort_order", "is_active"})


class StaleVersion(Exception):
    """Otra escritura cambió el motivo (o el pipeline) después de la versión que se editaba."""


class CodeTaken(Exception):
    """Ya hay un motivo (o un pipeline, o una etapa del pipeline) con ese código."""


def _code(value: str, pattern: str, fold: Callable[[str], str]) -> str:
    text = fold(value.strip()) if value.isascii() else ""
    if not (len(text) <= CODE_MAX and re.fullmatch(pattern, text)):
        raise ValueError(f"código con letras sin acento, cifras y guiones, hasta {CODE_MAX}")
    return text


def _integer(value: Any, low: int, high: int, what: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{what}: un entero de {low} a {high}")


def _diff(obj: Any, values: dict[str, Any]) -> dict[str, list[Any]]:
    return {f: [getattr(obj, f), v] for f, v in values.items() if getattr(obj, f) != v}


def _save(ctx: TenantContext, alias: str, obj: Any, changes: dict[str, list[Any]]) -> None:
    for field, (_, value) in changes.items():
        setattr(obj, field, value)
    obj.updated_by_user_id = ctx.user_id
    obj.version = F("version") + 1
    obj.save(using=alias, update_fields=[*changes, "updated_by_user_id", "version", "updated_at"])
    obj.refresh_from_db(using=alias, fields=["version"])


def _taken(error: IntegrityError, *constraints: str) -> Exception:
    if not any(c in str(error) for c in constraints):
        raise error
    return CodeTaken()


def create_lost_reason(
    ctx: TenantContext, *, name: str, code: str, sort_order: int | None = None
) -> LostReason:
    """Crea un motivo de pérdida, activo, y lo audita. Sin `sort_order`, va al final. `CodeTaken`
    si ya hay uno vivo con ese código (`MAYÚSCULAS_Y_GUION_BAJO`); sin escribir nada."""
    alias = require_scope(ctx)
    values: dict[str, Any] = {
        "name": clean_name(name or "", NAME_MAX),
        "code": _code(code, REASON_CODE, str.upper),
    }
    if sort_order is not None:
        _integer(sort_order, -32768, 32767, "orden")
        values["sort_order"] = sort_order
    try:
        with transaction.atomic(using=alias):
            if sort_order is None:
                last = LostReason.objects.using(alias).aggregate(last=Max("sort_order"))["last"]
                values["sort_order"] = 0 if last is None else last + 1
            reason: LostReason = LostReason.objects.using(alias).create(
                **values, created_by_user_id=ctx.user_id, updated_by_user_id=ctx.user_id
            )
            changes = {k: [None, v] for k, v in values.items()}
            record(
                ctx, "lost_reason.created", Entity("lost_reason", reason.pk, reason.code), changes
            )
    except IntegrityError as error:
        raise _taken(error, "lost_reasons_code_uq") from None
    return reason


def update_lost_reason(
    ctx: TenantContext, *, lost_reason_id: UUID, version: int, **fields: Any
) -> LostReason:
    """Cambia el nombre, el orden o si está activo, si nadie lo cambió desde `version`, y lo
    audita con el antes y el después; sin cambios no escribe. `StaleVersion`; `DoesNotExist` si no
    es de la organización o está borrado. El código no cambia."""
    alias = require_scope(ctx)
    if unknown := set(fields) - _REASON_FIELDS:
        raise ValueError(f"campos no admitidos: {sorted(unknown)}")
    values = dict(fields)
    if "name" in values:
        values["name"] = clean_name(values["name"] or "", NAME_MAX)
    if "sort_order" in values:
        _integer(values["sort_order"], -32768, 32767, "orden")
    with transaction.atomic(using=alias):
        live = LostReason.objects.using(alias).filter(deleted_at__isnull=True)
        reason: LostReason = live.select_for_update(no_key=True).get(pk=lost_reason_id)
        if reason.version != version:
            raise StaleVersion(lost_reason_id)
        if changes := _diff(reason, values):
            _save(ctx, alias, reason, changes)
            record(
                ctx, "lost_reason.updated", Entity("lost_reason", reason.pk, reason.code), changes
            )
    return reason
