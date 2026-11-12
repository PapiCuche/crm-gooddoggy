"""F5-09c (revisión de #709): la solicitud guarda también quién la envió (`submitted_by_user_id`).
Una de la IA no tiene a nadie que la pida (`requested_by_user_id` nulo, lo exige un `CHECK`), y
quien enviaba el borrador de la IA podía aprobarlo. Ahora quien aprueba no es quien pidió ni quien
envió: el `CHECK` de separación de funciones lo dice y el disparador (con `CREATE OR REPLACE`:
`0002` ya está fusionada) no deja cambiar quién envió, exige al crearla que quien envió sea quien
actúa (`app.user_id`) y no aprueba una sin quien envió (el `CHECK` con un nulo no dice nada).

**Qué no hace:** `app.user_id` es una variable local a la transacción que fija `tenant_scope`:
la regla protege de un fallo de la aplicación, no de un SQL hostil de `crm_app`, que podría fijarla.
Las solicitudes anteriores quedan con nulo: una abierta no se aprueba tras el despliegue (se
cancela y se vuelve a pedir). Volver atrás borra la columna y sus valores. El `CHECK` y la FK nuevos
se validan al crearse, con un bloqueo de la tabla mientras la recorren; en una tabla grande se
haría con `NOT VALID` y un `VALIDATE` aparte."""

from importlib import import_module
from typing import Any

from django.conf import settings
from django.db import migrations, models

PREVIOUS = import_module("apps.quotes.migrations.0002_approvals_and_discount_policies")
_fail = PREVIOUS._fail
OLD = "     OR NEW.requested_by_user_id IS DISTINCT FROM OLD.requested_by_user_id\n"
STARTS = "    RETURN NEW;\n  END IF;\n  IF OLD.status <> 'PENDING' THEN\n"
ENDS = "  RETURN NEW;\nEND $f$"
SUBMITTER = _fail("quote_approvals_submitter", "quote_approvals: the submitter is who acts")
if any(PREVIOUS.APPROVALS_GUARD.count(x) != 1 for x in (OLD, STARTS, ENDS)):
    raise RuntimeError("0004: the 0002 guard changed, the replacement would do nothing")
GUARD = (
    PREVIOUS.APPROVALS_GUARD.replace(
        OLD, OLD + "     OR NEW.submitted_by_user_id IS DISTINCT FROM OLD.submitted_by_user_id\n"
    )
    .replace(STARTS, f"""    IF NEW.submitted_by_user_id IS NULL OR NEW.submitted_by_user_id
       IS DISTINCT FROM nullif(current_setting('app.user_id', true), '')::uuid THEN
      {SUBMITTER}
    END IF;
{STARTS}""")
    .replace(ENDS, f"""  IF NEW.status = 'APPROVED' AND NEW.submitted_by_user_id IS NULL THEN
    {SUBMITTER}
  END IF;
{ENDS}""")
)  # fmt: skip


def _replace(apps: Any, schema_editor: Any) -> None:
    sql = GUARD.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION")
    schema_editor.execute(sql.replace("__APP__", settings.DB_APP_ROLE))


def _restore(apps: Any, schema_editor: Any) -> None:
    sql = PREVIOUS.APPROVALS_GUARD.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION")
    schema_editor.execute(sql.replace("__APP__", settings.DB_APP_ROLE))


class Migration(migrations.Migration):
    dependencies = [
        ("quotes", "0003_quotes_guard_limit"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="quoteapproval",
            name="quote_approvals_separation_ck",
        ),
        migrations.AddField(
            model_name="quoteapproval",
            name="submitted_by_user_id",
            field=models.UUIDField(blank=True, null=True),
        ),
        migrations.AddConstraint(
            model_name="quoteapproval",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("decided_by_user_id", models.F("requested_by_user_id")),
                        ("status", "APPROVED"),
                        _negated=True,
                    ),
                    models.Q(
                        ("decided_by_user_id", models.F("submitted_by_user_id")),
                        ("status", "APPROVED"),
                        _negated=True,
                    ),
                    models.Q(
                        models.Q(("status", "REJECTED"), _negated=True),
                        ("comment__regex", "\\S"),
                        _connector="OR",
                    ),
                ),
                name="quote_approvals_separation_ck",
            ),
        ),
        PREVIOUS.fk(
            "quote_approvals",
            "quote_approvals_submitted_by_fk",
            "organization_id, submitted_by_user_id",
            PREVIOUS.MEMBERS,
        ),
        migrations.RunPython(_replace, _restore),
    ]
