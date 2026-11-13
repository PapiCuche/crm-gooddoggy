"""F7-09a2b: `message_type` `IMAGE`, `AUDIO` y `VIDEO` para los medios entrantes (ADR-029 §7), y
solo entrantes, como `UNSUPPORTED`, hasta los salientes de F7-09b.

`messages` es la tabla más grande: los dos `CHECK` nuevos se añaden `NOT VALID`. Quitar y añadir
un `CHECK` pide un bloqueo `ACCESS EXCLUSIVE`, pero solo cambia el catálogo (no recorre la tabla),
así que dura lo que tarda en conseguirse; con `lock_timeout` de 5 s la migración falla y se repite
en vez de dejar en cola, detrás de una transacción larga, todas las lecturas y escrituras. Los dos
rigen ya para toda fila nueva o modificada; la `0025` los valida (bloqueo `SHARE UPDATE EXCLUSIVE`,
que no impide leer ni escribir). Hacia atrás vuelven los `CHECK` de `0010`, que recorren la tabla y
fallan si ya hay mensajes de un medio (no se pierde nada)."""

from django.db import migrations, models

BEFORE = ("TEXT", "NOTE", "UNSUPPORTED")
MEDIA = ("IMAGE", "AUDIO", "VIDEO")
TABLE = "public.messages"
TYPE, ONLY_IN = "messages_type_ck", "messages_unsupported_ck"


def listed(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def sql(types: tuple[str, ...], inbound_only: tuple[str, ...], tail: str) -> list[str]:
    return [
        f"ALTER TABLE {TABLE} DROP CONSTRAINT {TYPE}",
        f"ALTER TABLE {TABLE} DROP CONSTRAINT {ONLY_IN}",
        f"ALTER TABLE {TABLE} ADD CONSTRAINT {TYPE} CHECK "
        f"(message_type IN ({listed(types)})){tail}",
        f"ALTER TABLE {TABLE} ADD CONSTRAINT {ONLY_IN} CHECK (NOT (message_type IN "
        f"({listed(inbound_only)})) OR direction = 'INBOUND'){tail}",
    ]


class Migration(migrations.Migration):
    dependencies = [
        ("conversations", "0022_message_attachments_claim_clock"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RemoveConstraint(model_name="message", name=TYPE),
                migrations.RemoveConstraint(model_name="message", name=ONLY_IN),
                migrations.AlterField(
                    model_name="message",
                    name="message_type",
                    field=models.CharField(
                        choices=[
                            ("TEXT", "Text"),
                            ("NOTE", "Note"),
                            ("UNSUPPORTED", "Unsupported"),
                            ("IMAGE", "Image"),
                            ("AUDIO", "Audio"),
                            ("VIDEO", "Video"),
                        ],  # fmt: skip
                        default="TEXT",
                        max_length=16,
                    ),
                ),
                migrations.AddConstraint(
                    model_name="message",
                    constraint=models.CheckConstraint(
                        condition=models.Q(("message_type__in", [*BEFORE, *MEDIA])), name=TYPE
                    ),
                ),
                migrations.AddConstraint(
                    model_name="message",
                    constraint=models.CheckConstraint(
                        condition=models.Q(
                            models.Q(("message_type__in", ("UNSUPPORTED", *MEDIA)), _negated=True),
                            ("direction", "INBOUND"),
                            _connector="OR",
                        ),
                        name=ONLY_IN,
                    ),
                ),
            ],
            database_operations=[
                migrations.RunSQL(
                    [
                        "SET LOCAL lock_timeout = '5s'",
                        *sql((*BEFORE, *MEDIA), ("UNSUPPORTED", *MEDIA), " NOT VALID"),
                    ],
                    ["SET LOCAL lock_timeout = '5s'", *sql(BEFORE, ("UNSUPPORTED",), "")],
                ),  # fmt: skip
            ],
        ),
    ]
