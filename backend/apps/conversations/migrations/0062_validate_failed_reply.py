"""F7b-14: valida los dos `CHECK` y la FK que la `0061` añadió `NOT VALID` (`VALIDATE CONSTRAINT`
recorre la tabla con un bloqueo que no impide leer ni escribir; `lock_timeout` de 5 s que se quita
aunque falle; hacia atrás quedan, validados) y marca `last_reply_failed` en las conversaciones
cuya última respuesta ya falló: por tandas de `BATCH`, cada una en su transacción y con sus filas
bloqueadas antes de mirar los mensajes (una respuesta nueva a la vez espera y gana). Lo último es
nuestro, el cliente escribió y ninguna respuesta desde su último mensaje pudo llegar (`FAILED`
salvo `PROVIDER_UNKNOWN`). Necesita BYPASSRLS (crm_migrator). Las solicitudes de persona que una
respuesta fallida ya cerró antes de F7b-14 no se pueden reabrir: se borraron (la auditoría guarda
la historia). **No atómica**, como la `0054`."""

from typing import Any

from django.db import migrations, transaction

NAMES = ("conversations_human_reply_ck", "conversations_reply_failed_ck",
         "conversations_human_reply_fk")  # fmt: skip
BATCH = 500
PICK = (
    "SELECT id FROM public.conversations WHERE id > %s AND last_message_direction = 'OUTBOUND'"
    " AND NOT last_reply_failed ORDER BY id LIMIT %s FOR NO KEY UPDATE"
)
MARK = """
UPDATE public.conversations c SET last_reply_failed = true
WHERE c.id = ANY(%s) AND EXISTS (
  SELECT 1 FROM public.messages i WHERE i.organization_id = c.organization_id
    AND i.conversation_id = c.id AND i.direction = 'INBOUND'
) AND NOT EXISTS (
  SELECT 1 FROM public.messages o WHERE o.organization_id = c.organization_id
    AND o.conversation_id = c.id AND o.direction = 'OUTBOUND'
    AND (o.delivery_status <> 'FAILED' OR o.provider_error_code = 'PROVIDER_UNKNOWN')
    AND o.created_at > (
      SELECT max(i.created_at) FROM public.messages i WHERE i.organization_id = c.organization_id
        AND i.conversation_id = c.id AND i.direction = 'INBOUND'))
"""


def validate(apps: Any, schema_editor: Any) -> None:
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SET lock_timeout = '5s'")
        try:
            for name in NAMES:
                cursor.execute(f"ALTER TABLE public.conversations VALIDATE CONSTRAINT {name}")
        finally:
            cursor.execute("RESET lock_timeout")


def mark(apps: Any, schema_editor: Any) -> None:
    connection = schema_editor.connection
    with connection.cursor() as cursor:
        cursor.execute("SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user")
        if not cursor.fetchone()[0]:
            raise RuntimeError(
                "conversations 0062: run as crm_migrator with BYPASSRLS (RLS hides rows)"
            )
    last = "00000000-0000-0000-0000-000000000000"
    while True:
        with transaction.atomic(using=connection.alias), connection.cursor() as cursor:
            cursor.execute("SET LOCAL lock_timeout = '5s'")
            cursor.execute(PICK, [last, BATCH])
            ids = [row[0] for row in cursor.fetchall()]
            if not ids:
                return
            cursor.execute(MARK, [ids])
            last = str(ids[-1])


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("conversations", "0061_conversation_failed_reply"),
    ]

    operations = [
        migrations.RunPython(validate, migrations.RunPython.noop),
        migrations.RunPython(mark, migrations.RunPython.noop),
    ]
