"""Lecturas de precios, por organización (RLS y `TenantManager`). El permiso de la ruta lo exige
quien las sirve (`HasPermission`); la visibilidad de cada lista, estas lecturas."""

from collections.abc import Iterable
from datetime import datetime
from uuid import UUID

from django.db.models import Q, QuerySet

from apps.access.selectors import ExecutionContext, has_permission
from apps.pricing.models import READ_PERMISSIONS, PriceList, ProductPrice

MANAGE = "prices.manage"


def visible_price_lists(ectx: ExecutionContext) -> QuerySet[PriceList]:
    """Las listas no borradas que el actor puede ver: las que piden un permiso de lectura que
    tiene, o todas con `prices.manage`. Una que no ve no se distingue de una que no existe. El
    orden lo pone la paginación (ADR-016)."""
    lists = PriceList.objects.filter(deleted_at__isnull=True)
    if has_permission(ectx, MANAGE):
        return lists
    held = [code for code in READ_PERMISSIONS if has_permission(ectx, code)]
    return lists.filter(required_permission__in=held)


def _pair(variant_id: UUID, price_list_id: UUID) -> QuerySet[ProductPrice]:
    return ProductPrice.objects.filter(variant_id=variant_id, price_list_id=price_list_id)


def _ruling(at: datetime) -> Q:
    """`starts_at <= at < ends_at`: el inicio incluido, el fin excluido (NULL: sin fin)."""
    return Q(starts_at__lte=at) & (Q(ends_at__isnull=True) | Q(ends_at__gt=at))


def price_at(variant_id: UUID, price_list_id: UUID, at: datetime) -> ProductPrice | None:
    """El precio que rige en `at`. Sin comprobar la visibilidad de la lista: la decide quien
    pregunta (el motor, `services`)."""
    found: ProductPrice | None = _pair(variant_id, price_list_id).filter(_ruling(at)).first()
    return found


def prices_at(
    variant_ids: Iterable[UUID], price_list_id: UUID, at: datetime
) -> QuerySet[ProductPrice]:
    """Los precios que rigen en `at` para varias variantes de una lista, en una consulta: como
    mucho uno por variante (la `EXCLUDE`). Tampoco comprueba la visibilidad."""
    rows = ProductPrice.objects.filter(variant_id__in=variant_ids, price_list_id=price_list_id)
    return rows.filter(_ruling(at))


def next_price(variant_id: UUID, price_list_id: UUID, after: datetime) -> ProductPrice | None:
    """El siguiente precio programado: el primero que empieza después de `after`."""
    rows = _pair(variant_id, price_list_id).filter(starts_at__gt=after)
    found: ProductPrice | None = rows.order_by("starts_at").first()
    return found


def price_history(ectx: ExecutionContext, variant_id: UUID) -> QuerySet[ProductPrice]:
    """El historial de una variante en las listas que el actor ve, por lista y del más nuevo al
    más antiguo, con el motivo y `created_by_user_id`: el nombre del autor lo resuelve quien
    tenga `users.view`, como en el resto de pantallas (no se filtra con `prices.view`)."""
    rows = ProductPrice.objects.filter(
        variant_id=variant_id, price_list_id__in=visible_price_lists(ectx).values("id")
    )
    return rows.order_by("price_list_id", "-starts_at")
