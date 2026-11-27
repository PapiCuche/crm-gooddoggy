"""F11-04: rellena `awaiting_reply_since` de las conversaciones que ya esperaban respuesta antes
de la `0066` (solo las que la imagen anterior no volvió a escribir desde entonces). Por tandas de
`BATCH`, cada una en su transacción, con `lock_timeout` de 5 s y sus filas bloqueadas
(`FOR NO KEY UPDATE`): una escritura a la vez espera o gana, y la fila que ya tiene su hora no se
vuelve a tocar. La hora la calcula el disparador de la `0066` (paso 4: el primer mensaje del
cliente después de la última respuesta que cuenta); esta migración solo le pide que la mire, con
una escritura que no cambia nada más. Necesita BYPASSRLS (crm_migrator): solo corre al migrar,
nunca desde la aplicación. Se puede repetir. **No atómica**, como la `0062`; hacia atrás, nada."""

from typing import Any

from django.db import migrations, transaction

BATCH = 500
PICK = (  # «Sin responder» (`selectors.UNANSWERED`) y todavía sin hora
    "SELECT id FROM public.conversations WHERE id > %s AND (last_message_direction = 'INBOUND'"
    " OR last_reply_failed) AND status NOT IN ('RESOLVED', 'CLOSED')"
    " AND awaiting_reply_since IS NULL ORDER BY id LIMIT %s FOR NO KEY UPDATE"
)
FILL = (  # el disparador pone la hora: `NULL` es solo lo que la fila ya tiene
    "UPDATE public.conversations SET awaiting_reply_since = NULL WHERE id = ANY(%s)"
    " AND (last_message_direction = 'INBOUND' OR last_reply_failed)"
    " AND status NOT IN ('RESOLVED', 'CLOSED') AND awaiting_reply_since IS NULL"
)


def fill(apps: Any, schema_editor: Any) -> None:
    connection = schema_editor.connection
    with connection.cursor() as cursor:
        cursor.execute("SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user")
        if not cursor.fetchone()[0]:
            raise RuntimeError(
                "conversations 0068: run as crm_migrator with BYPASSRLS (RLS hides rows)"
            )
    last = "00000000-0000-0000-0000-000000000000"
    while True:
        with transaction.atomic(using=connection.alias), connection.cursor() as cursor:
            cursor.execute("SET LOCAL lock_timeout = '5s'")
            cursor.execute(PICK, [last, BATCH])
            ids = [row[0] for row in cursor.fetchall()]
            if not ids:
                return
            cursor.execute(FILL, [ids])
            last = str(ids[-1])


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("conversations", "0067_conversations_awaiting_reply_index"),
    ]

    operations = [
        migrations.RunPython(fill, migrations.RunPython.noop),
    ]
