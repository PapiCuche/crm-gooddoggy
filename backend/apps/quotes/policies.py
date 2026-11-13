"""Políticas de descuento por rol (F5-09b4; 04 §L.3): fijar el límite de un rol, en porcentaje y,
si se quiere, en importe con su moneda, para una lista de precios o para todas. Solo
`apps.quotes.api` lo importa (import-linter).

Comprueban `discount_policies.manage` antes que nada y que nadie da más de lo que tiene
(`AccessDenied`): el Owner, o quien tiene una política propia, para esa lista o para todas, con ese
porcentaje o más y, si lleva tope en importe, uno igual o mayor en la misma moneda. Crear exige
cubrir el límite nuevo; tocar una política que ya existe (cambiarla o quitarla), también el que
tenía: quien tiene un 10 % no rebaja a un rol de 100 %. Sin política propia no se toca ninguna.
El rol Owner no tiene política: no tiene límite. Quien no cubre el porcentaje (y el tope en
importe, si se pide) para esa lista no aprende nada de roles ni listas: el 403 va antes de los 400
de referencias; quitar una política que no existe es, para quien no lo cubre todo, el mismo 403
que quitar una que no cubre (F5-09b5). Con su versión y `Decimal`
exacto, auditan el antes y el después (`discount_policy.changed`); sin cambios no escriben ni
auditan.

**Efecto:** inmediato y sobre lo que viene: los comandos de descuento y el disparador de `quotes`
leen la política al medir. Una cotización que ya salió de `DRAFT` no se vuelve a medir (bajar un
límite no deshace una aprobada o enviada); un borrador que ya no cabe pide aprobación al enviar."""

from decimal import Decimal
from typing import Any
from uuid import UUID

from django.db import IntegrityError, transaction
from django.db.models import F, Q

from apps.access.models import MembershipRole, Role
from apps.access.selectors import AccessDenied, Denied, execution_context, has_permission
from apps.audit.services import Entity, record
from apps.pricing.services import price_list_currency
from apps.quotes.models import DiscountPolicy
from core.money import currency as check_currency
from core.money import parse_amount
from core.tenancy.context import TenantContext
from core.tenancy.scope import require_scope

MANAGE = "discount_policies.manage"
FIELDS = ("max_discount_percent", "max_discount_amount", "currency")
NOT_ROLE = "no es un rol de la organización con un límite que fijar"
NOT_LIST = "no es una lista de precios de la organización"
_RACE: dict[str, tuple[str, str] | None] = {  # restricción violada → campo (None: ya existe)
    "discount_policies_all_lists_uq": None, "discount_policies_list_uq": None,
    "discount_policies_role_fk": ("role_id", NOT_ROLE),
    "discount_policies_price_list_fk": ("price_list_id", NOT_LIST),
}  # fmt: skip


class Invalid(ValueError):
    """Un dato que no sirve; `field` es el campo del cuerpo (400)."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


class DuplicatePolicy(Exception):
    """Ya hay una política de ese rol para esa lista (o para todas): se cambia con su versión."""


class StalePolicy(Exception):
    """La política cambió, o ya no existe, desde la versión que se leyó."""


def _number(field: str, value: Any, top: int | None = None) -> Decimal:
    """Un importe o porcentaje de una persona: texto o `Decimal`, con 2 decimales, hasta `top`."""
    try:
        number = parse_amount(value)
    except TypeError, ValueError:
        raise Invalid(field, "un número con hasta 2 decimales") from None
    if top is not None and number > top:
        raise Invalid(field, f"entre 0 y {top}")
    return number


def _currency(
    ectx: Any, list_id: UUID | None, cap: Decimal | None, wanted: str | None
) -> str | None:
    """La moneda del tope: la de la lista si hay lista (y si se dice, es esa); sin lista, la que se
    diga, que va con el tope y solo con él (la base exige moneda con una lista y con un tope)."""
    if wanted is not None:
        try:
            check_currency(wanted)
        except TypeError, ValueError:
            raise Invalid("currency", "un código ISO 4217 de tres letras mayúsculas") from None
    if list_id is None:
        if (wanted is None) != (cap is None):
            raise Invalid("currency", "va con el tope en importe, y solo con él")
        return wanted
    found = price_list_currency(ectx, list_id)
    if found is None:
        raise Invalid("price_list_id", NOT_LIST)
    if wanted not in (None, found):
        raise Invalid("currency", "la de la lista de precios")
    return found


def _held(alias: str, ectx: Any) -> tuple[bool, Any]:
    """Si quien actúa es el Owner, y las políticas de sus roles."""
    roles = MembershipRole.objects.using(alias).filter(membership_id=ectx.membership_id)
    roles = roles.values("role_id")
    owner = Role.objects.using(alias).filter(pk__in=roles, is_owner_role=True).exists()
    return owner, DiscountPolicy.objects.using(alias).filter(role_id__in=roles)


def _covers(alias: str, ectx: Any, list_id: UUID | None, percent: Decimal, cap: Decimal | None,
            money: str | None) -> bool:  # fmt: skip
    """Quien actúa es el Owner o tiene una política que admite este límite para esa lista (la de
    esa lista o la de todas; si la pedida es la de todas, solo la de todas)."""
    owner, held = _held(alias, ectx)
    fits = Q(max_discount_amount=None)
    if cap is not None:
        fits |= Q(currency=money, max_discount_amount__gte=cap)
    held = held.filter(
        Q(applies_to_price_list_id=None) | Q(applies_to_price_list_id=list_id),
        max_discount_percent__gte=percent,
    )  # fmt: skip
    return owner or held.filter(fits).exists()


def _holds(alias: str, ectx: Any, row: DiscountPolicy) -> bool:
    """Quien actúa cubre el límite que `row` tiene hoy."""
    listed = row.applies_to_price_list_id
    return _covers(
        alias, ectx, listed, row.max_discount_percent, row.max_discount_amount, row.currency
    )


def _shown(row: DiscountPolicy) -> dict[str, str | None]:
    return {k: None if (v := getattr(row, k)) is None else str(v) for k in FIELDS}


def _audit(ctx: TenantContext, alias: str, row: DiscountPolicy, how: str, changes: Any) -> None:
    role, listed = Role.objects.using(alias).get(pk=row.role_id), row.applies_to_price_list_id
    metadata = {"operation": how, "role": role.code, "price_list_id": listed and str(listed)}
    record(ctx, "discount_policy.changed", Entity("discount_policy", row.pk, role.name),
           changes, metadata)  # fmt: skip


def _create(
    ctx: TenantContext, alias: str, role_id: UUID, list_id: UUID | None, fresh: dict[str, Any]
) -> DiscountPolicy:
    try:
        with transaction.atomic(using=alias):  # un savepoint: perder una carrera es un 409
            row: DiscountPolicy = DiscountPolicy.objects.using(alias).create(
                role_id=role_id, applies_to_price_list_id=list_id, **fresh
            )
    except IntegrityError as error:
        name = getattr(getattr(error.__cause__, "diag", None), "constraint_name", None)
        if name not in _RACE:  # cualquier otra restricción es un fallo, no un dato del cliente
            raise
        field = _RACE[name]  # el mismo texto que sin carrera: ajena e inexistente no se distinguen
        if field is None:
            raise DuplicatePolicy from None
        raise Invalid(*field) from None
    _audit(ctx, alias, row, "created", {k: [None, v] for k, v in _shown(row).items()})
    return row


def _change(
    ctx: TenantContext, alias: str, row: DiscountPolicy, fresh: dict[str, Any]
) -> DiscountPolicy:
    before = _shown(row)
    for name, value in fresh.items():
        setattr(row, name, value)
    if changed := {k: [before[k], v] for k, v in _shown(row).items() if before[k] != v}:
        row.version = F("version") + 1
        row.save(using=alias, update_fields=[*FIELDS, "version", "updated_at"])
        row.refresh_from_db(using=alias)
        _audit(ctx, alias, row, "updated", changed)
    return row


def _authorized(ctx: TenantContext) -> Any:
    ectx = execution_context(ctx)
    if not has_permission(ectx, MANAGE):
        raise AccessDenied(Denied.PERMISSION)
    return ectx


def set_discount_policy(
    ctx: TenantContext, *, role_id: UUID, max_discount_percent: str | Decimal,
    price_list_id: UUID | None = None, max_discount_amount: str | Decimal | None = None,
    currency: str | None = None, version: int | None = None,
) -> DiscountPolicy:  # fmt: skip
    """Fija el límite de `role_id` para esa lista (o todas). Sin `version` crea la política
    (`DuplicatePolicy` si ya hay una); con ella cambia la que hay (`StalePolicy` si cambió o ya no
    existe). `Invalid` con su campo; `AccessDenied`."""
    alias = require_scope(ctx)
    with transaction.atomic(using=alias):
        ectx = _authorized(ctx)
        percent = _number("max_discount_percent", max_discount_percent, 100)
        cap = (
            None
            if max_discount_amount is None
            else _number("max_discount_amount", max_discount_amount)
        )
        if version is not None and (type(version) is not int or version < 1):
            raise Invalid("version", "un entero desde 1")
        owner, held = _held(alias, ectx)  # quien no llega a esto en esa lista no aprende nada más
        held = held.filter(
            Q(applies_to_price_list_id=None) | Q(applies_to_price_list_id=price_list_id),
            max_discount_percent__gte=percent,
        )  # fmt: skip
        if cap is None:  # sin tope pedido, el importe es ilimitado: solo lo cubre quien no lo tiene
            held = held.filter(max_discount_amount=None)
        else:  # con lista, su moneda aún no se mira: la cifra; sin lista, también la moneda
            same = Q(max_discount_amount__gte=cap)
            same &= Q(currency=currency) if price_list_id is None else Q()
            held = held.filter(Q(max_discount_amount=None) | same)
        if not (owner or held.exists()):
            raise AccessDenied(Denied.ESCALATION)
        rows = DiscountPolicy.objects.using(alias).select_for_update(no_key=True)
        row = rows.filter(role_id=role_id, applies_to_price_list_id=price_list_id).first()
        if row is not None and not _holds(alias, ectx, row):  # antes de decir qué es válido
            raise AccessDenied(Denied.ESCALATION)
        role = Role.objects.using(alias).filter(pk=role_id).first()
        if role is None or role.is_owner_role:  # el Owner no tiene límite: no hay nada que fijar
            raise Invalid("role_id", NOT_ROLE)
        money = _currency(ectx, price_list_id, cap, currency)
        if not _covers(alias, ectx, price_list_id, percent, cap, money):
            raise AccessDenied(Denied.ESCALATION)
        fresh = {"max_discount_percent": percent, "max_discount_amount": cap, "currency": money}
        if row is None:
            if version is not None:
                raise StalePolicy
            return _create(ctx, alias, role.pk, price_list_id, fresh)
        if version is None:
            raise DuplicatePolicy
        if row.version != version:
            raise StalePolicy
        return _change(ctx, alias, row, fresh)


def delete_discount_policy(ctx: TenantContext, *, policy_id: UUID, version: int) -> None:
    """Quita la política: el rol vuelve a 0 %, o al límite de sus otros roles. `StalePolicy` si
    cambió desde `version`; `AccessDenied` si no cubre su límite. Una que no existe (o de otra
    organización) es `DoesNotExist` solo para quien lo cubre todo (100 %, todas las listas, sin
    tope); para el resto, el mismo `AccessDenied`: no se distingue por su id."""
    alias = require_scope(ctx)
    with transaction.atomic(using=alias):
        ectx = _authorized(ctx)
        rows = DiscountPolicy.objects.using(alias).select_for_update(no_key=True)
        row = rows.filter(pk=policy_id).first()
        if row is None and _covers(alias, ectx, None, Decimal(100), None, None):
            raise DiscountPolicy.DoesNotExist("DiscountPolicy matching query does not exist.")
        if row is None or not _holds(alias, ectx, row):
            raise AccessDenied(Denied.ESCALATION)
        if row.version != version:
            raise StalePolicy
        _audit(ctx, alias, row, "deleted", {k: [v, None] for k, v in _shown(row).items()})
        row.delete(using=alias)
