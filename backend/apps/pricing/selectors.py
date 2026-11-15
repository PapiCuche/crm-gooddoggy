"""Lecturas de precios, por organización (RLS y `TenantManager`). El permiso de la ruta lo exige
quien las sirve (`HasPermission`); la visibilidad de cada lista, estas lecturas."""

from collections.abc import Iterable
from datetime import datetime
from uuid import UUID

from django.db.models import Exists, OuterRef, Q, QuerySet

from apps.access.selectors import ExecutionContext, has_permission
from apps.pricing.models import (
    ALERTING,
    READ_PERMISSIONS,
    PriceChangeBatch,
    PriceChangeBatchItem,
    PriceList,
    ProductPrice,
    Promotion,
    PromotionItem,
    PromotionPriceList,
)

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


def visible_batches(ectx: ExecutionContext) -> QuerySet[PriceChangeBatch]:
    """Los lotes de edición masiva de las listas que el actor ve, activas o no (como el historial):
    uno de una lista oculta no se distingue de uno que no existe, porque sus líneas enseñan precios
    (F3-49b). El permiso de la ruta lo pide quien lo sirve."""
    lists = visible_price_lists(ectx).values("id")
    return PriceChangeBatch.objects.filter(price_list_id__in=lists)


def lined_batches(ectx: ExecutionContext) -> QuerySet[PriceChangeBatch]:
    """Los lotes cuyas líneas se enseñan: los visibles de una lista que sigue activa. Las de una
    lista desactivada no, como sus precios en `…/price-lists/{id}/prices/` (F3-49c)."""
    return visible_batches(ectx).filter(price_list__is_active=True)


def batch_lines(
    ectx: ExecutionContext, batch_id: UUID, *, status: str | None = None, alert: bool | None = None
) -> QuerySet[PriceChangeBatchItem]:
    """Las líneas de un lote de `lined_batches`, con su variante; `alert` con la regla de
    `summary.alerts` (`ALERTING`). Ninguna si el lote no se enseña: el 404 lo decide quien sirve."""
    shown = lined_batches(ectx).filter(pk=batch_id).values("id")
    rows = PriceChangeBatchItem.objects.filter(batch_id__in=shown).select_related("variant")
    if status is not None:
        rows = rows.filter(status=status)
    if alert is not None:
        rows = rows.filter(ALERTING) if alert else rows.exclude(ALERTING)
    return rows


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


def next_prices(
    variant_ids: Iterable[UUID], price_list_id: UUID, after: datetime
) -> QuerySet[ProductPrice]:
    """`next_price` para varias variantes de una lista, en una consulta: como mucho uno por
    variante (`DISTINCT ON`)."""
    rows = ProductPrice.objects.filter(variant_id__in=variant_ids, price_list_id=price_list_id)
    return (
        rows.filter(starts_at__gt=after).order_by("variant_id", "starts_at").distinct("variant_id")
    )


def price_history(ectx: ExecutionContext, variant_id: UUID) -> QuerySet[ProductPrice]:
    """El historial de una variante en las listas que el actor ve (la ruta lo pagina por `-id`;
    este orden solo vale sin paginar), con el motivo y `created_by_user_id`: el nombre del autor
    lo resuelve quien tenga `users.view`, como en el resto de pantallas (no se filtra con
    `prices.view`)."""
    rows = ProductPrice.objects.filter(
        variant_id=variant_id, price_list_id__in=visible_price_lists(ectx).values("id")
    )
    return rows.order_by("price_list_id", "-starts_at")


def promotions_from(
    at: datetime, price_list: PriceList, channel: str | None, *, ai_only: bool
) -> QuerySet[Promotion]:
    """Las promociones publicadas y no borradas que aplican a la lista (y su moneda) y al canal y
    que no han terminado en `at`: las vigentes y las que empiezan después (el motor mira cuándo
    cambia la respuesta). Sin canal, solo las de todos los canales. Sin comprobar la visibilidad
    de la lista: la decide el motor."""
    rows = Promotion.objects.filter(
        status="PUBLISHED",
        deleted_at__isnull=True,
        ends_at__gt=at,
        currency=price_list.currency,
        id__in=PromotionPriceList.objects.filter(price_list_id=price_list.pk).values(
            "promotion_id"
        ),
    )
    rows = rows.filter(
        Q(channels__isnull=True) | Q(channels__contains=[channel])
        if channel
        else Q(channels__isnull=True)
    )
    return rows.filter(ai_exposable=True) if ai_only else rows


def promotion_items(promotion_ids: Iterable[UUID]) -> QuerySet[PromotionItem]:
    """Los objetivos de varias promociones, en una consulta."""
    return PromotionItem.objects.filter(promotion_id__in=promotion_ids)


def visible_promotions(ectx: ExecutionContext) -> QuerySet[Promotion]:
    """Las promociones no borradas que el actor puede ver: todas con `prices.manage`; si no, solo
    las publicadas cuyas listas ve todas y están activas (una oculta o inactiva la oculta, como
    si no existiera, igual que `…/price-lists/{id}/prices/`)."""
    rows = Promotion.objects.filter(deleted_at__isnull=True)
    if has_permission(ectx, MANAGE):
        return rows
    hidden = PromotionPriceList.objects.filter(promotion_id=OuterRef("pk")).exclude(
        price_list_id__in=visible_price_lists(ectx).filter(is_active=True).values("id")
    )
    return rows.filter(status="PUBLISHED").exclude(Exists(hidden))


def promotion_lists(promotion_ids: Iterable[UUID]) -> QuerySet[PromotionPriceList]:
    """Las listas de varias promociones, en una consulta."""
    return PromotionPriceList.objects.filter(promotion_id__in=promotion_ids)
