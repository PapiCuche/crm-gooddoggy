"""F3-30: el ledger admite las dos patas de una transferencia.

- `inventory_movements_sign_ck` añade `TRANSFER_OUT` (< 0) y `TRANSFER_IN` (> 0).
- `inventory_movements_transfer_ck` (nuevo): `transfer_group_id` no nulo si y solo si el tipo es
  `TRANSFER_*`, y entonces `reference_type = 'TRANSFER'` y `reference_id = transfer_group_id`;
  ningún otro tipo lleva `reference_type = 'TRANSFER'`. Los `IS NOT NULL` explícitos impiden que un
  nulo deje pasar el `CHECK`.

Antes de tocar nada, si alguna fila existente los violara, la migración se niega y dice cuántas (lo
comprueba `crm_migrator`, que ha de tener `BYPASSRLS`: con FORCE RLS no vería ninguna). El ledger
crece sin límite: los dos se añaden `NOT VALID` (cambiar un `CHECK` pide `ACCESS EXCLUSIVE`, pero
solo toca el catálogo) con `lock_timeout` de 5 s, para fallar y repetirse en vez de dejar en cola
lecturas y escrituras detrás de una transacción larga. Rigen ya para toda fila nueva; la `0005`
los valida. Hacia atrás vuelve el `CHECK` de `0003` (más laxo: no puede fallar) y se quita el
nuevo."""

from typing import Any

from django.db import migrations, models

TABLE = "public.inventory_movements"
SIGN, TRANSFER = "inventory_movements_sign_ck", "inventory_movements_transfer_ck"
TYPES = "movement_type IN ('TRANSFER_OUT', 'TRANSFER_IN')"
OLD_SIGN = (
    "NOT (movement_type = 'RECEIPT' AND quantity <= 0)"
    " AND NOT (movement_type = 'ISSUE' AND quantity >= 0)"
    " AND NOT (movement_type = 'RESERVE' AND quantity <= 0)"
    " AND NOT (movement_type = 'RELEASE' AND quantity >= 0)"
)
NEW_SIGN = (
    f"{OLD_SIGN} AND NOT (movement_type = 'TRANSFER_OUT' AND quantity >= 0)"
    " AND NOT (movement_type = 'TRANSFER_IN' AND quantity <= 0)"
)
GROUP = (
    f"({TYPES} AND transfer_group_id IS NOT NULL AND reference_type = 'TRANSFER'"
    " AND reference_id IS NOT NULL AND reference_id = transfer_group_id)"
    f" OR (NOT ({TYPES}) AND transfer_group_id IS NULL AND reference_type <> 'TRANSFER')"
)
LOCK = "SET LOCAL lock_timeout = '5s'"


def _scalar(schema_editor: Any, sql: str) -> Any:
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(sql)
        return cursor.fetchone()[0]


def refuse_violations(apps: Any, schema_editor: Any) -> None:
    """Una fila que ya violara los `CHECK` nuevos haría fallar la `0005`: se resuelve antes."""
    if not _scalar(schema_editor, "SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user"):
        raise RuntimeError("inventory 0004: run as crm_migrator with BYPASSRLS (RLS hides rows)")
    count = _scalar(  # `NOT (…)` es nulo donde el `CHECK` pasaría: cuenta lo mismo que rechaza
        schema_editor,
        f"SELECT count(*) FROM {TABLE} WHERE NOT ({NEW_SIGN}) OR NOT ({GROUP})",  # noqa: S608
    )
    if count:
        raise RuntimeError(
            f"inventory 0004: {count} inventory movements violate the transfer checks;"
            " fix them first (docs/phases/phase-3.md, F3-30)"
        )


def _sql(sign: str, tail: str, *, add_transfer: bool) -> tuple[str, ...]:
    steps = (
        LOCK,
        f"ALTER TABLE {TABLE} DROP CONSTRAINT {SIGN}",
        f"ALTER TABLE {TABLE} ADD CONSTRAINT {SIGN} CHECK ({sign}){tail}",
    )
    if add_transfer:
        return (*steps, f"ALTER TABLE {TABLE} ADD CONSTRAINT {TRANSFER} CHECK ({GROUP}){tail}")
    return (LOCK, f"ALTER TABLE {TABLE} DROP CONSTRAINT {TRANSFER}", *steps[1:])


TRANSFER_TYPES = ["TRANSFER_OUT", "TRANSFER_IN"]


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0003_reservations"),
    ]

    operations = [
        migrations.RunPython(refuse_violations, migrations.RunPython.noop),
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RemoveConstraint(model_name="inventorymovement", name=SIGN),
                migrations.AddConstraint(
                    model_name="inventorymovement",
                    constraint=models.CheckConstraint(
                        condition=models.Q(
                            models.Q(
                                ("movement_type", "RECEIPT"), ("quantity__lte", 0), _negated=True
                            ),
                            models.Q(
                                ("movement_type", "ISSUE"), ("quantity__gte", 0), _negated=True
                            ),
                            models.Q(
                                ("movement_type", "RESERVE"), ("quantity__lte", 0), _negated=True
                            ),
                            models.Q(
                                ("movement_type", "RELEASE"), ("quantity__gte", 0), _negated=True
                            ),
                            models.Q(
                                ("movement_type", "TRANSFER_OUT"),
                                ("quantity__gte", 0),
                                _negated=True,
                            ),
                            models.Q(
                                ("movement_type", "TRANSFER_IN"),
                                ("quantity__lte", 0),
                                _negated=True,
                            ),
                        ),  # fmt: skip
                        name=SIGN,
                    ),
                ),
                migrations.AddConstraint(
                    model_name="inventorymovement",
                    constraint=models.CheckConstraint(
                        condition=models.Q(
                            models.Q(
                                ("movement_type__in", TRANSFER_TYPES),
                                ("reference_id", models.F("transfer_group_id")),
                                ("reference_id__isnull", False),
                                ("reference_type", "TRANSFER"),
                                ("transfer_group_id__isnull", False),
                            ),
                            models.Q(
                                models.Q(("movement_type__in", TRANSFER_TYPES), _negated=True),
                                ("transfer_group_id__isnull", True),
                                models.Q(("reference_type", "TRANSFER"), _negated=True),
                            ),
                            _connector="OR",
                        ),
                        name=TRANSFER,
                    ),
                ),
            ],
            database_operations=[
                migrations.RunSQL(
                    _sql(NEW_SIGN, " NOT VALID", add_transfer=True),
                    _sql(OLD_SIGN, "", add_transfer=False),
                ),
            ],
        ),
    ]
