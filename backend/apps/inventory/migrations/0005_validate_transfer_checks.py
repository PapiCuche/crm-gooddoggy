"""F3-30: valida los dos `CHECK` de `inventory_movements` que la `0004` añadió `NOT VALID`.
`VALIDATE CONSTRAINT` recorre la tabla con un bloqueo `SHARE UPDATE EXCLUSIVE`, que no impide leer
ni escribir; va en su propia migración (su propia transacción) para no recorrerla bajo el bloqueo
exclusivo de la `0004`. Hacia atrás no hace nada (los `CHECK` quedan, validados)."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0004_transfer_checks"),
    ]

    operations = [
        migrations.RunSQL(
            [
                f"ALTER TABLE public.inventory_movements VALIDATE CONSTRAINT {name}"
                for name in ("inventory_movements_sign_ck", "inventory_movements_transfer_ck")
            ],
            migrations.RunSQL.noop,
        ),  # fmt: skip
    ]
