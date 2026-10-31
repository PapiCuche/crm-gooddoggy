"""F5-01: el pipeline minorista y los motivos de pérdida habituales de las organizaciones que ya
existían (también `pruebas`); las nuevas los reciben del alta (`apps.provisioning.seeds`).

Corre como `crm_migrator` (BYPASSRLS, ADR-002 §1.1), pero no depende de ello: fija el tenant de
cada organización, así que también valdría con FORCE RLS sin BYPASSRLS. Es autónoma: lleva su
propia copia de los datos y de las columnas de la `0001` y usa los modelos históricos, no
`apps.deals.seed`, para que un cambio posterior de tablas o de semilla no rompa `migrate` en una
base nueva. Es idempotente sin bloqueos (uno por organización agotaría la memoria de bloqueos de
la transacción con decenas de miles; los índices únicos protegen la carrera): no toca a quien ya
tiene pipeline vivo o motivos. Sin auditoría (no hay actor). Al revertir no borra nada: las tablas
se retiran con la `0001` y `crm_app` no puede borrar filas.
"""

from typing import Any

from django.db import migrations

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


def seed_existing(apps: Any, schema_editor: Any) -> None:
    alias = schema_editor.connection.alias
    pipelines, stages, reasons = (
        apps.get_model("deals", name).objects.using(alias)
        for name in ("Pipeline", "PipelineStage", "LostReason")
    )
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SELECT id FROM organizations ORDER BY created_at, id")
        ids = [row[0] for row in cursor.fetchall()]
        for organization_id in ids:
            cursor.execute("SELECT set_config('app.tenant_id', %s, true)", [str(organization_id)])
            here: dict[str, Any] = {"organization_id": organization_id}
            if not pipelines.filter(**here, deleted_at__isnull=True).exists():
                pipeline = pipelines.create(
                    **here, name="Ventas minoristas", code="retail", kind="RETAIL",
                    currency="PEN", is_default=True,
                )  # fmt: skip
                stages.bulk_create(
                    stages.model(
                        **here, pipeline_id=pipeline.pk, name=name, code=code, sort_order=order,
                        stage_type=kind, default_probability=probability,
                        stale_after_days=stale, color=color,
                    )
                    for order, (name, code, kind, probability, stale, color) in enumerate(STAGES)
                )  # fmt: skip
            if not reasons.filter(**here).exists():
                reasons.bulk_create(
                    reasons.model(**here, name=name, code=code, sort_order=order)
                    for order, (name, code) in enumerate(LOST_REASONS)
                )


class Migration(migrations.Migration):
    dependencies = [
        ("deals", "0001_pipelines"),
        ("organizations", "0010_invitations_are_not_deleted"),
    ]
    operations = [migrations.RunPython(seed_existing, migrations.RunPython.noop)]
