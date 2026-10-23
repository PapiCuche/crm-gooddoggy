"""Comandos de listas de precios (F3-40, E03-01): crear y cambiar.

No comprueban permisos: los declarará la API (`prices.manage`, F3-40b); fuera de `apps.pricing`
nadie los importa (contrato de import-linter). Validan lo que guardan y auditan cada cambio,
también el de la lista que deja de ser la predeterminada. El código y la moneda no cambian. Una
sola predeterminada por moneda, activa y con `prices.view` (03 §H, 04 §K.2): la primera lista así
de una moneda lo es, y elegir otra la cambia en la misma transacción.
"""

import re
from typing import Any
from uuid import UUID

from django.db import IntegrityError, connections, transaction

from apps.audit.services import Entity, record
from apps.pricing.models import DISTRIBUTOR, INTERNAL, VIEW, WHOLESALE, PriceList
from core.tenancy.context import TenantContext
from core.tenancy.scope import require_scope
from core.text import name as clean_name

CODE = re.compile(r"[A-Z0-9]+(-[A-Z0-9]+)*")  # la misma forma que impone la tabla
CODE_MAX, NAME_MAX = 30, 100
CURRENCIES = ("PEN",)  # D-PRC-2: solo PEN en el MVP; la columna ya admite otras
CODE_TAKEN = "price_lists_code_uq"
UPDATABLE = frozenset(
    {
        "name",
        "kind",
        "is_default",
        "required_permission",
        "ai_exposable",
        "payment_condition",
        "is_active",
        "sort_order",
    }
)
BY_KIND: dict[str, str] = {
    PriceList.Kind.WHOLESALE: WHOLESALE,
    PriceList.Kind.DISTRIBUTOR: DISTRIBUTOR,
    PriceList.Kind.INTERNAL: INTERNAL,
}


class PriceListCodeTaken(Exception):
    """Ya hay una lista con ese código en la organización."""


class PriceListIsDefault(Exception):
    """Es la predeterminada de su moneda: no se desactiva ni deja de serlo sin elegir otra."""


class InactiveDefault(ValueError):
    """Una lista inactiva no puede ser la predeterminada."""


class RestrictedDefault(ValueError):
    """La predeterminada la ve quien ve precios: pide `prices.view`."""


def price_list_code(value: str) -> str:
    """El código como se guarda: en mayúsculas ASCII, cifras y guiones entre ellas."""
    code = value.strip().upper() if value.isascii() else ""
    if not (len(code) <= CODE_MAX and CODE.fullmatch(code)):
        raise ValueError(f"letras sin acento, cifras y guiones entre ellas, hasta {CODE_MAX}")
    return code


def price_list_name(value: str) -> str:
    return clean_name(value, NAME_MAX)


def required_permission_for(kind: str) -> str:
    """El permiso de lectura que pide una lista de ese tipo si no se elige otro."""
    return BY_KIND.get(kind, VIEW)


def _cleaned(fields: dict[str, Any]) -> dict[str, Any]:
    if unknown := set(fields) - UPDATABLE:
        raise ValueError(f"campos no admitidos: {sorted(unknown)}")
    return {k: price_list_name(v) if k == "name" else v for k, v in fields.items()}


def _lock(alias: str, ctx: TenantContext) -> None:
    """Un escritor de listas a la vez por organización, hasta el fin de la transacción: sin él,
    dos primeras listas a la vez se creerían las dos la predeterminada. Son escrituras raras."""
    with connections[alias].cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            [f"price_lists:{ctx.organization_id}"],
        )


def _live(alias: str) -> Any:
    return PriceList.objects.using(alias).filter(deleted_at__isnull=True)


def _has_default(alias: str, currency: str) -> bool:
    return bool(_live(alias).filter(currency=currency, is_default=True).exists())


def _drop_default(ctx: TenantContext, alias: str, currency: str) -> None:
    """La predeterminada actual de la moneda deja de serlo, con su auditoría."""
    for other in (
        _live(alias).select_for_update(no_key=True).filter(currency=currency, is_default=True)
    ):
        other.is_default, other.updated_by_user_id = False, ctx.user_id
        other.save(using=alias, update_fields=["is_default", "updated_by_user_id", "updated_at"])
        entity = Entity("price_list", other.pk, other.code)
        record(ctx, "price_list.updated", entity, {"is_default": [True, False]})


def create_price_list(
    ctx: TenantContext,
    *,
    code: str,
    name: str,
    kind: str,
    currency: str = "PEN",
    required_permission: str | None = None,
    is_default: bool | None = None,
    **fields: Any,
) -> PriceList:
    """Crea una lista y la audita. Es la predeterminada si se pide o, sin decirlo, si es la
    primera activa con `prices.view` de su moneda; pedirlo quita la marca a la anterior.
    `PriceListCodeTaken`, `InactiveDefault` o `RestrictedDefault` sin escribir nada."""
    alias = require_scope(ctx)
    if currency not in CURRENCIES:
        raise ValueError(f"solo {', '.join(CURRENCIES)}")
    values = {
        "code": price_list_code(code),
        "kind": kind,
        "currency": currency,
        "required_permission": required_permission or required_permission_for(kind),
        **_cleaned({"name": name, **fields}),
    }
    active, general = values.get("is_active", True), values["required_permission"] == VIEW
    if is_default and not active:
        raise InactiveDefault("is_default")
    if is_default and not general:
        raise RestrictedDefault("is_default")
    try:
        with transaction.atomic(using=alias):  # savepoint: todo o nada
            _lock(alias, ctx)
            if is_default:
                _drop_default(ctx, alias, currency)
            if is_default is None:  # sin decirlo: la primera que la ve quien ve precios
                is_default = active and general and not _has_default(alias, currency)
            values["is_default"] = is_default
            price_list: PriceList = PriceList.objects.using(alias).create(
                **values, created_by_user_id=ctx.user_id, updated_by_user_id=ctx.user_id
            )
            entity = Entity("price_list", price_list.pk, price_list.code)
            record(ctx, "price_list.created", entity, {k: [None, v] for k, v in values.items()})
    except IntegrityError as error:
        if CODE_TAKEN in str(error):
            raise PriceListCodeTaken(values["code"]) from None
        raise
    return price_list


def update_price_list(ctx: TenantContext, *, price_list_id: UUID, **fields: Any) -> PriceList:
    """Cambia lo que se envía y lo audita con el antes y el después. `is_default=true` quita la
    marca a la anterior de la moneda; activar una lista con `prices.view` cuando su moneda no tiene
    predeterminada la hace serlo, salvo `is_default=false` (otro cambio no la marca).
    `PriceListIsDefault` si se desactiva la predeterminada, se le quita la marca o se cambia su
    permiso; `InactiveDefault` o `RestrictedDefault` si se marca una inactiva o sin
    `prices.view`. Sin cambios no escribe ni audita. Una borrada o de otra organización:
    `DoesNotExist`."""
    alias = require_scope(ctx)
    values = _cleaned(fields)
    with transaction.atomic(using=alias):
        _lock(alias, ctx)
        price_list: PriceList = _live(alias).select_for_update(no_key=True).get(pk=price_list_id)
        changes = {
            field: [getattr(price_list, field), value]
            for field, value in values.items()
            if getattr(price_list, field) != value
        }
        active = values.get("is_active", price_list.is_active)
        default = values.get("is_default", price_list.is_default)
        general = values.get("required_permission", price_list.required_permission) == VIEW
        if price_list.is_default and not (default and active and general):
            raise PriceListIsDefault(price_list_id)
        if default and not active:
            raise InactiveDefault("is_default")
        if default and not general:
            raise RestrictedDefault("is_default")
        if default and not price_list.is_default:
            _drop_default(ctx, alias, price_list.currency)
        elif "is_default" not in values and "is_active" in changes and active and general:
            if not _has_default(alias, price_list.currency):  # se activa la primera así
                changes["is_default"] = [False, True]
        if changes:
            for field, (_, value) in changes.items():
                setattr(price_list, field, value)
            price_list.updated_by_user_id = ctx.user_id
            price_list.save(
                using=alias, update_fields=[*changes, "updated_by_user_id", "updated_at"]
            )
            entity = Entity("price_list", price_list.pk, price_list.code)
            record(ctx, "price_list.updated", entity, changes)
    return price_list
