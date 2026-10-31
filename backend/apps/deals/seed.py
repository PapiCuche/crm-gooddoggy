"""Lo que toda organización tiene desde el primer día (F5-01): el pipeline minorista por defecto y
los motivos de pérdida habituales (02 §E.6, D-COM-7).

Idempotente y sin permisos: la llama el alta (`apps.provisioning.seeds`) para las organizaciones
nuevas; las que ya existían las siembra la migración `0002`, con su propia copia congelada de
estos datos. No audita: el alta de la organización ya lo hace. No pisa lo que la organización
cambió: solo siembra el pipeline si no tiene ninguno vivo y los motivos si nunca tuvo ninguno (ni
siquiera borrado). Una organización con un pipeline vivo que no es el predeterminado no recibe
ninguno por aquí: lo evita el servicio de F5-01c, que marca el primero.
"""

from uuid import UUID

from django.db import connections, transaction

from apps.deals.models import LostReason, Pipeline, PipelineStage
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


def ensure_default_pipeline(ctx: TenantContext) -> bool:
    """Deja la organización del contexto con su pipeline predeterminado y sus motivos de
    pérdida. Devuelve si creó el pipeline."""
    alias = require_scope(ctx)
    here = {"organization_id": ctx.organization_id}
    with transaction.atomic(using=alias):
        lock_pipelines(alias, ctx.organization_id)
        pipelines = Pipeline.objects.using(alias)
        created = not pipelines.filter(deleted_at__isnull=True).exists()
        if created:
            pipeline = pipelines.create(
                name="Ventas minoristas", code="retail", kind="RETAIL", currency="PEN",
                is_default=True,
            )  # fmt: skip
            PipelineStage.objects.using(alias).bulk_create(
                PipelineStage(
                    **here, pipeline=pipeline, name=name, code=code, sort_order=order,
                    stage_type=kind, default_probability=probability, stale_after_days=stale,
                    color=color,
                )
                for order, (name, code, kind, probability, stale, color) in enumerate(STAGES)
            )  # fmt: skip
        if not LostReason.objects.using(alias).exists():
            LostReason.objects.using(alias).bulk_create(
                LostReason(**here, name=name, code=code, sort_order=order)
                for order, (name, code) in enumerate(LOST_REASONS)
            )
    return created
