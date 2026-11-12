"""F7-09a1b: `UNIQUE (organization_id, id)` en `messages`, el destino de la FK compuesta de los
adjuntos de mensajes (`0021`). Migración **no atómica**: el índice se construye con `CREATE UNIQUE
INDEX CONCURRENTLY`, que no bloquea lecturas ni escrituras mientras recorre la tabla, y después
`ADD CONSTRAINT ... USING INDEX` lo convierte en restricción: ese paso toma un `ACCESS EXCLUSIVE`
breve (no recorre la tabla, solo cambia el catálogo). `messages` es la tabla más grande del Inbox:
el recorrido concurrente puede tardar minutos con millones de filas sin bloquear, y el `ACCESS
EXCLUSIVE` espera a que acaben las transacciones en curso sobre la tabla (las que llegan detrás
esperan con él) y, concedido, dura milisegundos. Si el índice concurrente falla, queda `INVALID`:
se borra a mano (`DROP INDEX CONCURRENTLY messages_org_id_uq`) y se repite. Hacia atrás: quitar la
restricción borra también su índice."""

from django.db import migrations, models

INDEX = "messages_org_id_uq"


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("conversations", "0019_validate_error_code_check"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddConstraint(
                    model_name="message",
                    constraint=models.UniqueConstraint(
                        fields=("organization_id", "id"), name=INDEX
                    ),
                ),
            ],
            database_operations=[
                migrations.RunSQL(
                    f"CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS {INDEX} "
                    "ON public.messages (organization_id, id)",
                    f"DROP INDEX CONCURRENTLY IF EXISTS public.{INDEX}",
                ),
                migrations.RunSQL(
                    f"ALTER TABLE public.messages ADD CONSTRAINT {INDEX} "
                    f"UNIQUE USING INDEX {INDEX}",
                    f"ALTER TABLE public.messages DROP CONSTRAINT IF EXISTS {INDEX}",
                ),
            ],
        ),
    ]
