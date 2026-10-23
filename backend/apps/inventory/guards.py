"""Lo que el inventario guarda de una variante y el catálogo no puede borrar (F3-12).

`catalog` está debajo y no importa `inventory`: avisa con `catalog.events.variants_retiring`
con las variantes ya bloqueadas como las bloquea `stock.move` (`FOR NO KEY UPDATE`), así que el
saldo que se lee aquí es el último confirmado y ningún movimiento lo cambia hasta el final del
borrado. El ledger no se toca: el borrado es lógico.
"""

from typing import Any
from uuid import UUID

from django.db.models import Q

from apps.catalog.events import VariantsHeld
from apps.inventory.models import InventoryLevel


def refuse_with_stock(sender: Any, *, using: str, variant_ids: list[UUID], **kwargs: Any) -> None:
    """`VariantsHeld` si alguna de las variantes tiene existencias o reservas en algún almacén."""
    held = Q(on_hand__gt=0) | Q(reserved__gt=0)
    rows = InventoryLevel.objects.using(using).filter(held, variant_id__in=variant_ids)
    if rows.exists():
        raise VariantsHeld(variant_ids)
