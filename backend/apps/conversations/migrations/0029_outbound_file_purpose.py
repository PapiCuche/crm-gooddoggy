"""F7-09b2: el propósito `message-media-out`, el archivo que una persona sube para enviar (el
estado del modelo, en `files.0003`; el `CHECK` va aquí, con la regla de los mensajes que lo usan,
para que deshacer y rehacer `conversations` lo lleve consigo).

`files_values_ck` lo admite; `files_key_shape_ck` ya ata la clave al propósito de la fila, así que
un archivo `message-media-out` vive bajo `org/{org}/message-media-out/…` y nunca es el blob de un
medio entrante (`message-media`). El reclamo de un envío (`commands.claim_outbound`) exige este
propósito: un mensaje saliente que apuntara a un archivo `message-media` fallaría al enviarse. Antes
de tocar nada, si ya hubiera alguno, la migración se niega y dice cuántos (lo cuenta `crm_migrator`,
que ha de tener `BYPASSRLS`: con FORCE RLS no vería ninguno); con `MEDIA_SEND_UNSCANNED` apagado
por omisión (D-F7-04) no puede haberlos. El `CHECK` nuevo se añade `NOT VALID` con `lock_timeout`
de 5 s (cambiarlo pide `ACCESS EXCLUSIVE`, solo de catálogo); la `0030` lo valida. Hacia atrás
vuelve el de `files.0001`, que falla con un archivo `message-media-out`: no se pierde nada.
"""

from typing import Any

from django.db import migrations

TABLE, NAME = "public.files", "files_values_ck"
OLD = ("message-media", "quote-pdf", "import", "avatar", "kb", "attachment")
NEW = (OLD[0], "message-media-out", *OLD[1:])
LOCK = "SET LOCAL lock_timeout = '5s'"


def values_ck(purposes: tuple[str, ...]) -> str:
    listed = ", ".join(f"'{p}'" for p in purposes)
    return (
        f"CHECK (purpose IN ({listed}) AND scan_status IN ('PENDING', 'CLEAN', 'INFECTED') "
        "AND sha256 ~ '^[0-9a-f]{64}$')"
    )


def _scalar(schema_editor: Any, sql: str) -> Any:
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(sql)
        return cursor.fetchone()[0]


def refuse_inbound_files(apps: Any, schema_editor: Any) -> None:
    """Un mensaje saliente sobre un archivo `message-media` quedaría varado: se resuelve antes."""
    if not _scalar(schema_editor, "SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user"):
        raise RuntimeError(
            "conversations 0029: run as crm_migrator with BYPASSRLS (RLS hides rows)"
        )
    count = _scalar(
        schema_editor,
        "SELECT count(*) FROM public.messages m JOIN public.files f "
        "ON f.organization_id = m.organization_id AND f.id = m.file_id "
        "WHERE m.direction = 'OUTBOUND' AND f.purpose = 'message-media'",
    )
    if count:
        raise RuntimeError(
            f"conversations 0029: {count} outbound messages point at a message-media file;"
            " fail or resolve them first (docs/phases/phase-7.md, F7-09b2)"
        )


class Migration(migrations.Migration):
    dependencies = [
        ("conversations", "0028_validate_outbound_media"),
        ("files", "0003_message_media_out"),
    ]

    operations = [
        migrations.RunPython(refuse_inbound_files, migrations.RunPython.noop),
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
