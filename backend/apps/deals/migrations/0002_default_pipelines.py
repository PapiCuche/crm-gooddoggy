"""F5-01: el pipeline minorista y los motivos de pérdida habituales de las organizaciones que ya
existían (también `pruebas`); las nuevas los reciben del alta (`apps.provisioning.seeds`).

Corre como `crm_migrator` (BYPASSRLS, ADR-002 §1.1), pero no depende de ello: fija el tenant de
cada organización, así que también valdría con FORCE RLS sin BYPASSRLS. Usa los modelos
históricos de esta migración y la misma `seed_organization` que el alta. Es idempotente: no toca
a quien ya tiene pipeline o motivos. Sin auditoría (no hay actor). Al revertir no borra nada: las
tablas se retiran con la `0001` y `crm_app` no puede borrar filas.
"""

from typing import Any

from django.db import migrations

from apps.deals.seed import seed_organization


def seed_existing(apps: Any, schema_editor: Any) -> None:
    alias = schema_editor.connection.alias
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SELECT id FROM organizations ORDER BY created_at, id")
        ids = [row[0] for row in cursor.fetchall()]
        for organization_id in ids:
            cursor.execute("SELECT set_config('app.tenant_id', %s, true)", [str(organization_id)])
            seed_organization(alias, organization_id, apps)


class Migration(migrations.Migration):
    dependencies = [("deals", "0001_pipelines")]
    operations = [migrations.RunPython(seed_existing, migrations.RunPython.noop)]
