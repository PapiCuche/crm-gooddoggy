"""F7b-06e (ADR-035 §3): una página de Facebook es de **una** organización, sea cual sea la
plataforma. `channel_accounts_external_uq` solo compara cuentas de la misma plataforma: la
organización A podía conectar Instagram sobre la página P (su `external_parent_id`) y la B
Messenger sobre la misma P (su `external_account_id`). La `EXCLUDE` compara la página de cada
cuenta social viva (`COALESCE(external_parent_id, external_account_id)`: los CHECK de las dos
plataformas fijan que Messenger no tiene padre e Instagram sí) y la rechaza si es de otra
organización; en la misma, Messenger e Instagram de una página conviven. Una restricción no pasa
por la RLS: protege aunque quien escribe no vea la fila de la otra organización.

Una `EXCLUDE` no admite `NOT VALID`: si ya hubiera una página en dos organizaciones, la migración
se niega antes de crearla y dice **cuántas**, sin ids (el error de PostgreSQL los nombraría en el
registro del despliegue); se desconecta una de cada par y se repite. Nunca borra nada. Necesita
BYPASSRLS (crm_migrator), como la `0040`, para ver las filas de todas las organizaciones.
`lock_timeout` de 5 s: el índice GiST se construye bajo un bloqueo exclusivo de una tabla pequeña.
Hacia atrás, borra la restricción. Crea `btree_gist` si falta (como pricing `0002`, que puede
correr después) y no la borra.
"""

from typing import Any

import django.contrib.postgres.constraints
import django.db.models.functions.comparison
from django.db import migrations, models

SHARED = """
SELECT count(DISTINCT COALESCE(a.external_parent_id, a.external_account_id))
FROM public.channel_accounts a
WHERE a.deleted_at IS NULL AND a.platform IN ('MESSENGER', 'INSTAGRAM') AND EXISTS (
    SELECT 1 FROM public.channel_accounts b
    WHERE b.deleted_at IS NULL AND b.platform IN ('MESSENGER', 'INSTAGRAM')
      AND COALESCE(b.external_parent_id, b.external_account_id)
          = COALESCE(a.external_parent_id, a.external_account_id)
      AND b.organization_id <> a.organization_id
)"""


def refuse_shared_pages(cursor: Any) -> None:
    """Se niega si una página viva ya es de dos organizaciones; solo dice cuántas."""
    cursor.execute("SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user")
    if not cursor.fetchone()[0]:
        raise RuntimeError(
            "conversations 0049: run as crm_migrator with BYPASSRLS (RLS hides rows)"
        )
    cursor.execute(SHARED)
    count = cursor.fetchone()[0]
    if count:
        raise RuntimeError(
            f"conversations 0049: {count} page(s) connected in more than one organization;"
            " disconnect one account of each before migrating"
        )


def _check(apps: Any, schema_editor: Any) -> None:
    with schema_editor.connection.cursor() as cursor:
        refuse_shared_pages(cursor)


class Migration(migrations.Migration):
    dependencies = [
        ("conversations", "0048_conversations_standby_index"),
    ]

    operations = [
        migrations.RunSQL("SET LOCAL lock_timeout = '5s'", "SET LOCAL lock_timeout = '5s'"),
        migrations.RunPython(_check, migrations.RunPython.noop),
        # la `EXCLUDE` compara texto y uuid en GiST. Sin depender del orden de las apps: pricing
        # también la crea, y hacia atrás no se borra (la comparten)
        migrations.RunSQL(
            "CREATE EXTENSION IF NOT EXISTS btree_gist", reverse_sql=migrations.RunSQL.noop
        ),
        migrations.AddConstraint(
            model_name="channelaccount",
            constraint=django.contrib.postgres.constraints.ExclusionConstraint(
                condition=models.Q(
                    ("deleted_at__isnull", True), ("platform__in", ("MESSENGER", "INSTAGRAM"))
                ),
                expressions=[
                    (
                        django.db.models.functions.comparison.Coalesce(
                            "external_parent_id", "external_account_id"
                        ),
                        "=",
                    ),
                    ("organization_id", "<>"),
                ],
                name="channel_accounts_page_owner_ex",
            ),
        ),
    ]
