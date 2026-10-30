"""Lecturas de la auditoría de tenant (F2-73)."""

from django.db.models import Q, QuerySet

from apps.audit.models import AuditLog

# Las filas de un costo llevan el importe anterior y el nuevo: quien lee la auditoría no las ve
# sin `product_cost.view` (ADR-006 §1, F3-46c). Por entidad y por prefijo de acción: un
# `cost.viewed` de otra entidad también queda fuera.
COST_ENTITY, COST_ACTIONS = "variant_cost", "cost."
COST_ROWS = Q(entity_type=COST_ENTITY) | Q(action__startswith=COST_ACTIONS)


def entries(*, with_costs: bool = False) -> QuerySet[AuditLog]:
    """Las filas de auditoría de la organización del `tenant_scope` activo. Filtra por
    organización; el permiso lo exige quien las sirve (`HasPermission`). Sin `with_costs`, que
    solo pide quien tiene `product_cost.view` y un segundo factor reciente, no salen las filas de
    costos: ninguna lectura de la auditoría debe saltarse este filtro."""
    rows = AuditLog.objects.all()
    return rows if with_costs else rows.exclude(COST_ROWS)
