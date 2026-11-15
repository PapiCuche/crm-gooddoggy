"""F7-15 (ADR-008): `files_values_ck` admite `scan_status = ERROR` (el estado del modelo, en
`files.0005`). Como la `0029`, va con `conversations` para que deshacer y rehacer esta app lo lleve
consigo. `NOT VALID` con `lock_timeout` de 5 s (solo catálogo); la `0036` lo valida. Hacia atrás
vuelve el de la `0029`, que falla mientras haya un `ERROR`: no se pierde nada."""

from django.db import migrations

TABLE, NAME = "public.files", "files_values_ck"
PURPOSES = (
    "'message-media', 'message-media-out', 'quote-pdf', 'import', 'avatar', 'kb', 'attachment'"
)
OLD, NEW = "'PENDING', 'CLEAN', 'INFECTED'", "'PENDING', 'CLEAN', 'INFECTED', 'ERROR'"
LOCK = "SET LOCAL lock_timeout = '5s'"


def values_ck(statuses: str) -> str:
    return (
        f"CHECK (purpose IN ({PURPOSES}) AND scan_status IN ({statuses}) "
        "AND sha256 ~ '^[0-9a-f]{64}$')"
    )


class Migration(migrations.Migration):
    dependencies = [
        ("conversations", "0034_credentials_created_by_member_fk"),
        ("files", "0005_scan_error"),
    ]

    operations = [
        migrations.RunSQL(
            [
                LOCK,
                f"ALTER TABLE {TABLE} DROP CONSTRAINT {NAME}",
                f"ALTER TABLE {TABLE} ADD CONSTRAINT {NAME} {values_ck(NEW)} NOT VALID",
            ],
            [
                LOCK,
                f"ALTER TABLE {TABLE} DROP CONSTRAINT {NAME}",
                f"ALTER TABLE {TABLE} ADD CONSTRAINT {NAME} {values_ck(OLD)}",
            ],
        ),  # fmt: skip
    ]
