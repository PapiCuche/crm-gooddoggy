"""F6-08a1: las categorías de respuestas rápidas de las organizaciones que ya existían; las nuevas
las reciben del alta (`apps.conversations.seed`).

Corre como `crm_migrator` (BYPASSRLS, ADR-002 §1.1), pero no depende de ello: fija el tenant de
cada organización, así que también valdría con FORCE RLS sin BYPASSRLS. Es autónoma: lleva su
propia copia de los nombres, para que un cambio posterior de la semilla no cambie `migrate` en una
base nueva. Idempotente: no toca a quien ya tuvo alguna categoría (también borrada). Sin auditoría
(no hay actor). Al revertir no borra nada: la tabla se retira con la `0037`.
"""

from typing import Any

from django.db import migrations

CATEGORIES = ("Ventas", "Precios", "Stock", "Delivery", "Garantías", "Pagos", "Atención general")
SEED = (
    "INSERT INTO quick_reply_categories (organization_id, name, sort_order, is_active, "
    "created_at, updated_at, version) "
    "SELECT %s, name, ord - 1, true, now(), now(), 1 FROM unnest(%s::text[]) "
    "WITH ORDINALITY AS c(name, ord) "
    "WHERE NOT EXISTS (SELECT 1 FROM quick_reply_categories WHERE organization_id = %s)"
)


def seed_existing(apps: Any, schema_editor: Any) -> None:
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SELECT id FROM organizations ORDER BY created_at, id")
        for (organization_id,) in cursor.fetchall():
            cursor.execute("SELECT set_config('app.tenant_id', %s, true)", [str(organization_id)])
            cursor.execute(SEED, [organization_id, list(CATEGORIES), organization_id])


class Migration(migrations.Migration):
    dependencies = [("conversations", "0037_quick_replies")]
    operations = [migrations.RunPython(seed_existing, migrations.RunPython.noop)]
