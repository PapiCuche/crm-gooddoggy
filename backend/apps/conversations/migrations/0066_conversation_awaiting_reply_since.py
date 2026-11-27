"""F11-04: `conversations.awaiting_reply_since`, desde cuándo una conversación espera respuesta
(ADR-049 §5). Una columna nula sin valor por omisión (solo catálogo, sin reescribir la tabla) y el
disparador que la escribe; nadie más la escribe: lo que mande la aplicación se ignora.

«Sin responder» es la regla de #1093 (`selectors.UNANSWERED`): lo último lo dijo el cliente o
ninguna respuesta desde su último mensaje pudo llegar (`last_reply_failed`), y no está resuelta
ni cerrada. El disparador `conversations_reply_wait` (BEFORE, INVOKER, para todo rol; su nombre va
después de `conversations_reply_guard`, de la `0061`, así que ve la marca ya corregida):
1. fuera de «Sin responder» la deja en `NULL` (una respuesta que cuenta, resuelta o cerrada);
2. si ya esperaba y tenía su hora, la conserva: el episodio sigue (otro mensaje del cliente, una
   respuesta que falló, una asignación);
3. si el episodio empieza con un mensaje del cliente, la hora de ese mensaje (`last_message_at`);
4. si no (una respuesta en cola que termina `FAILED`, o una fila de antes de esta migración), el
   primer mensaje del cliente después de la última respuesta que cuenta (no `FAILED`, o
   `PROVIDER_UNKNOWN`: pudo salir), con `messages_org_conversation_idx`; sin ninguno visible (sin
   el contexto de la organización, RLS oculta los mensajes), `last_message_at`.
Nunca rechaza una escritura, así que la imagen anterior (al migrar o tras volver atrás, ADR-033
§7) sigue escribiendo igual y la columna queda bien también con lo que ella escribe.

Aproximaciones conocidas (solo afectan a avisos internos; también en `docs/phases/phase-11.md`):
- a) el paso 4 no ve una resolución: si se resolvió sin responder, el cliente volvió a escribir y
  la respuesta siguiente falla, la hora es la del mensaje anterior a la resolución (dice más espera
  de la real);
- b) si el cliente escribe mientras una respuesta sigue en cola y luego esa respuesta falla, la
  conversación no se vuelve a escribir y la hora queda en el mensaje más reciente (dice menos);
- c) volver a «Sin responder» sin un mensaje nuevo (hoy ningún comando lo hace) toma
  `last_message_at`.

`lock_timeout` de 5 s: si no consigue el bloqueo, la migración falla entera (es atómica) y se
puede repetir. Hacia atrás: el disparador, la función y la columna."""

from django.db import migrations, models

WAITS = (
    "(%(r)s.last_message_direction IS NOT DISTINCT FROM 'INBOUND' OR %(r)s.last_reply_failed)"
    " AND %(r)s.status NOT IN ('RESOLVED', 'CLOSED')"
)
FUNCTION = f"""
CREATE FUNCTION public.conversations_reply_wait() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $fn$
DECLARE
    was boolean := false;
    since timestamptz;
BEGIN
    IF TG_OP = 'UPDATE' THEN  -- `OLD` solo existe al actualizar
        was := {WAITS % {"r": "OLD"}};
        IF was THEN
            since := OLD.awaiting_reply_since;
        END IF;
    END IF;
    IF NOT ({WAITS % {"r": "NEW"}}) THEN
        NEW.awaiting_reply_since := NULL;
        RETURN NEW;
    END IF;
    IF since IS NULL AND NOT was AND NEW.last_message_direction = 'INBOUND' THEN
        since := NEW.last_message_at;
    END IF;
    IF since IS NULL THEN
        since := coalesce((
            SELECT min(i.created_at) FROM public.messages i
            WHERE i.organization_id = NEW.organization_id AND i.conversation_id = NEW.id
              AND i.direction = 'INBOUND' AND i.created_at > coalesce((
                SELECT max(o.created_at) FROM public.messages o
                WHERE o.organization_id = NEW.organization_id AND o.conversation_id = NEW.id
                  AND o.direction = 'OUTBOUND' AND (o.delivery_status IS DISTINCT FROM 'FAILED'
                      OR o.provider_error_code = 'PROVIDER_UNKNOWN')
              ), '-infinity'::timestamptz)
        ), NEW.last_message_at);
    END IF;
    NEW.awaiting_reply_since := since;
    RETURN NEW;
END
$fn$;
REVOKE ALL ON FUNCTION public.conversations_reply_wait() FROM PUBLIC;
"""  # noqa: S608 — solo constantes del módulo
ADD = (
    "SET LOCAL lock_timeout = '5s'",
    "ALTER TABLE public.conversations ADD COLUMN awaiting_reply_since timestamptz NULL",
    FUNCTION,
    "CREATE TRIGGER conversations_reply_wait BEFORE INSERT OR UPDATE ON public.conversations"
    " FOR EACH ROW EXECUTE FUNCTION public.conversations_reply_wait()",
)
DROP = (
    "SET LOCAL lock_timeout = '5s'",
    "DROP TRIGGER conversations_reply_wait ON public.conversations",
    "DROP FUNCTION public.conversations_reply_wait()",
    "ALTER TABLE public.conversations DROP COLUMN awaiting_reply_since",
)


class Migration(migrations.Migration):
    dependencies = [
        ("conversations", "0065_credential_expiry_attempt"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddField(
                    model_name="conversation",
                    name="awaiting_reply_since",
                    field=models.DateTimeField(blank=True, editable=False, null=True),
                ),
            ],
            database_operations=[migrations.RunSQL(ADD, DROP)],
        ),
    ]
