"""Qué promoción rige para una variante (ADR-006 §2, pasos 5 a 7): lógica pura sobre lo que
`services` ya leyó, sin consultas. La vigencia es un cálculo con el instante, nunca un job."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from apps.pricing.models import DiscountType, Promotion, PromotionItem
from core.money import Rounding, round_amount

# La regla de la organización (paso 7) es `CENTS` mientras no exista su configuración.
ORGANIZATION_ROUNDING = Rounding.CENTS


@dataclass(frozen=True, slots=True)
class Target:
    """Lo que alcanza a una variante: ella, su producto, su marca y su categoría con las
    antecesoras (por `path`), cada categoría con la longitud de su `path` (más larga, más cerca)."""

    variant_id: UUID
    product_id: UUID
    brand_id: UUID | None
    categories: dict[UUID, int]


@dataclass(frozen=True, slots=True)
class Candidate:
    promotion: Promotion
    items: tuple[PromotionItem, ...]


@dataclass(frozen=True, slots=True)
class Applied:
    promotion: Promotion
    final_amount: Decimal


def _specificity(item: PromotionItem, target: Target) -> tuple[int, int] | None:
    """Cuán cerca está el objetivo de la variante: variante, producto, categoría (la más honda
    primero), marca. `None`: no la alcanza. Solo ordena los objetivos de una misma promoción."""
    if item.variant_id == target.variant_id:
        return (0, 0)
    if item.product_id == target.product_id:
        return (1, 0)
    if item.category_id in target.categories:
        return (2, -target.categories[item.category_id])
    if item.brand_id is not None and item.brand_id == target.brand_id:
        return (3, 0)
    return None


def reach(candidate: Candidate, target: Target) -> Decimal | None:
    """El valor con que la promoción alcanza a la variante (el `override_value` del objetivo más
    cercano, o su `value`), o `None` si no la alcanza."""
    best: tuple[tuple[int, int], PromotionItem] | None = None
    for item in candidate.items:
        rank = _specificity(item, target)
        if rank is not None and (best is None or rank < best[0]):
            best = (rank, item)
    if best is None:
        return None
    override = best[1].override_value
    return candidate.promotion.value if override is None else override


def price_with(kind: str, value: Decimal, base: Decimal) -> Decimal | None:
    """El precio con la promoción, redondeado, o `None` si no abarata: en 0 o menos, o sin bajar
    de la base. Nunca se recorta."""
    if kind == DiscountType.FIXED_PRICE:
        raw = value
    elif kind == DiscountType.PERCENT_OFF:
        raw = base * (Decimal(100) - value) / Decimal(100)
    else:
        raw = base - value
    final = round_amount(raw, ORGANIZATION_ROUNDING)
    return final if 0 < final < base else None


def select(
    candidates: list[Candidate], target: Target, at: datetime, base: Decimal
) -> Applied | None:
    """La de mayor `priority`; si empatan, la de menor precio final; nunca se apilan. La
    especificidad no desempata (ADR-006 §2, paso 6); el `id` solo hace estable el resultado."""
    best: tuple[tuple[int, Decimal, str], Applied] | None = None
    for candidate in candidates:
        promotion = candidate.promotion
        if not promotion.starts_at <= at < promotion.ends_at:
            continue
        value = reach(candidate, target)
        final = None if value is None else price_with(promotion.discount_type, value, base)
        if final is None:
            continue
        key = (-promotion.priority, final, str(promotion.pk))
        if best is None or key < best[0]:
            best = (key, Applied(promotion, final))
    return None if best is None else best[1]


def valid_until(
    candidates: list[Candidate],
    target: Target,
    at: datetime,
    base: Decimal,
    applied: Applied | None,
    price_ends_at: datetime | None,
) -> datetime | None:
    """El primero entre el fin del precio, el fin de la promoción aplicada y el inicio de la
    próxima promoción que, al empezar, cambiaría la respuesta (otra ganaría)."""
    bound = price_ends_at
    if applied is not None and (bound is None or applied.promotion.ends_at < bound):
        bound = applied.promotion.ends_at
    starts = sorted({c.promotion.starts_at for c in candidates if c.promotion.starts_at > at})
    for start in starts:
        if bound is not None and start >= bound:
            break
        then = select(candidates, target, start, base)
        if (then.promotion.pk if then else None) != (applied.promotion.pk if applied else None):
            return start
    return bound
