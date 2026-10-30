"""Lo que toda organización tiene desde el primer día (F5-01): el pipeline minorista por defecto y
los motivos de pérdida habituales (02 §E.6, D-COM-7).

Idempotente y sin permisos: la llama el alta (`apps.provisioning.seeds`, para las organizaciones
nuevas) y la migración `0002` (para las que ya existían), con los modelos de cada una: los vivos
o los históricos de la migración. No audita: el alta de la organización ya lo hace y la migración
no tiene actor. No pisa lo que la organización cambió: solo siembra el pipeline si no tiene
ninguno vivo y los motivos si nunca tuvo ninguno (ni siquiera borrado).
"""

from typing import Any
from uuid import UUID

from django.apps import apps as live_apps
from django.apps.registry import Apps
from django.db import connections, transaction

from core.tenancy.context import TenantContext
from core.tenancy.scope import require_scope

# (nombre, código, tipo, probabilidad, días hasta estancarse, color)
STAGES = (
    ("Nuevo", "nuevo", "OPEN", 10, None, "#64748b"),
    ("Contactado", "contactado", "OPEN", 20, None, "#0ea5e9"),
    ("Cotizado", "cotizado", "OPEN", 40, 7, "#f59e0b"),
    ("Negociación", "negociacion", "OPEN", 60, 5, "#8b5cf6"),
    ("Ganado", "ganado", "WON", 100, None, "#16a34a"),
    ("Perdido", "perdido", "LOST", 0, None, "#dc2626"),
)
LOST_REASONS = (
    ("Precio", "PRICE"),
    ("Sin stock", "OUT_OF_STOCK"),
    ("Compró en otro lugar", "COMPETITOR"),
    ("Sin respuesta", "NO_RESPONSE"),
    ("Ya no lo necesita", "CHANGED_MIND"),
    ("Otro", "OTHER"),
)


def lock_pipelines(alias: str, organization_id: UUID) -> None:
    """Un escritor de pipelines a la vez por organización, hasta el fin de la transacción: sin él,
    dos primeros pipelines (o dos «predeterminado») a la vez se creerían los dos el único."""
    with connections[alias].cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            [f"pipelines:{organization_id}"],
        )


def seed_organization(alias: str, organization_id: UUID, registry: Apps) -> bool:
    """Siembra una organización con los modelos de `registry`. Devuelve si creó el pipeline."""
    pipelines, stages, reasons = (
        registry.get_model("deals", name)._base_manager.using(alias)
        for name in ("Pipeline", "PipelineStage", "LostReason")
    )
    lock_pipelines(alias, organization_id)
    here: dict[str, Any] = {"organization_id": organization_id}
    created = not pipelines.filter(**here, deleted_at__isnull=True).exists()
    if created:
        pipeline = pipelines.create(
            **here, name="Ventas minoristas", code="retail", kind="RETAIL", currency="PEN",
            is_default=True,
        )  # fmt: skip
        stages.bulk_create(
            stages.model(
                **here, pipeline_id=pipeline.pk, name=name, code=code, sort_order=order,
                stage_type=kind, default_probability=probability, stale_after_days=stale,
                color=color,
            )
            for order, (name, code, kind, probability, stale, color) in enumerate(STAGES)
        )  # fmt: skip
    if not reasons.filter(**here).exists():
        reasons.bulk_create(
            reasons.model(**here, name=name, code=code, sort_order=order)
            for order, (name, code) in enumerate(LOST_REASONS)
        )
    return created


def ensure_default_pipeline(ctx: TenantContext) -> bool:
    """Deja la organización del contexto con su pipeline predeterminado y sus motivos de
    pérdida. Devuelve si creó el pipeline."""
    alias = require_scope(ctx)
    with transaction.atomic(using=alias):
        return seed_organization(alias, ctx.organization_id, live_apps)
