"""F7-09a1b: `UNIQUE (organization_id, id)` en `files`, el destino de la FK compuesta de los
adjuntos de mensajes (`conversations.0021`). Migración **no atómica**: el índice se construye con
`CREATE UNIQUE INDEX CONCURRENTLY`, que no bloquea lecturas ni escrituras mientras recorre la
tabla, y después `ADD CONSTRAINT ... USING INDEX` lo convierte en restricción: ese paso toma un
`ACCESS EXCLUSIVE` breve (no recorre la tabla: solo cambia el catálogo). Si el índice concurrente
falla, queda `INVALID`: se borra a mano (`DROP INDEX CONCURRENTLY files_org_id_uq`) y se repite.
Hacia atrás: quitar la restricción borra también su índice."""

from django.db import migrations, models

INDEX = "files_org_id_uq"


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("files", "0001_initial"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddConstraint(
                    model_name="file",
                    constraint=models.UniqueConstraint(
                        fields=("organization_id", "id"), name=INDEX
                    ),
                ),
            ],
            database_operations=[
                migrations.RunSQL(
                    f"CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS {INDEX} "
                    "ON public.files (organization_id, id)",
                    f"DROP INDEX CONCURRENTLY IF EXISTS public.{INDEX}",
                ),
                migrations.RunSQL(
                    f"ALTER TABLE public.files ADD CONSTRAINT {INDEX} UNIQUE USING INDEX {INDEX}",
                    f"ALTER TABLE public.files DROP CONSTRAINT IF EXISTS {INDEX}",
                ),
            ],
        ),
    ]
