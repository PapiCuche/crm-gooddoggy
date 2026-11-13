"""F5-09c3: la respuesta del cliente (aceptar o rechazar, con su fecha y el motivo del rechazo) y
el vencimiento, protegidos en la base. Dos `CHECK` (para todo rol): `ACCEPTED` lleva `accepted_at` y
`REJECTED` lleva `rejected_at` y un motivo con texto, las dos dentro de la validez
(`sent_at <= fecha < valid_until`); cualquier otro estado, ninguno de los tres. El disparador de
`quotes` se reemplaza con `CREATE OR REPLACE` (`0005` ya está fusionada) **añadiendo un bloque a su
cuerpo vigente**, que antes se compara con el de `0005` (si no es ese, no se toca nada), en un punto
que ha de aparecer una sola vez. Para `crm_app`:

- la fecha y el motivo de la respuesta solo se escriben al pasar de `SENT` o `VIEWED` a `ACCEPTED`
  o `REJECTED`, y después no cambian (`IS DISTINCT FROM`: admiten nulos);
- responder exige que la validez no haya pasado (`valid_until > now()`, el reloj de la
  transacción) y una persona (`app.actor_type = 'USER'`): la IA no registra respuestas;
- vencer (`EXPIRED`) exige que haya pasado (`valid_until <= now()`).

**Qué no hace:** no comprueba `quotes.send` ni su alcance (lo hacen los comandos); `app.actor_type`
es una variable de la transacción que fija `tenant_scope`: un SQL hostil de `crm_app` podría
fijarla, y fechar una respuesta en cualquier punto de la validez.

**Datos que ya existen.** Una aceptada o rechazada sin fecha (solo por SQL directo tras `0005`) no
tiene una fecha que inventar: la migración se niega y dice cuántas son (hay que resolverlas antes).
Lo comprueba `crm_migrator`, que ha de tener `BYPASSRLS` (si no, se niega: bajo RLS no las vería),
también al volver atrás.
Volver atrás se niega si alguna lleva fecha de respuesta; si no, compara la función instalada con
la de esta migración y restaura exactamente la de `0005`."""

from importlib import import_module
from typing import Any

from django.conf import settings
from django.db import migrations, models

PREVIOUS = import_module("apps.quotes.migrations.0005_issue")
_fail = PREVIOUS.PREVIOUS._fail
COLUMNS = ("accepted_at", "rejected_at", "rejection_reason")
ANSWER = "OLD.status IN ('SENT', 'VIEWED') AND NEW.status IN ('ACCEPTED', 'REJECTED')"
BLOCK = f"""  IF ({" OR ".join(f"NEW.{c} IS DISTINCT FROM OLD.{c}" for c in COLUMNS)})
     AND NOT ({ANSWER}) THEN
    {_fail("quotes_answer_frozen", "quotes: an answer is written only when answering")}
  END IF;
  IF {ANSWER} AND NOT OLD.valid_until > now() THEN
    {_fail("quotes_answer_expired", "quotes: the validity is over")}
  END IF;
  IF {ANSWER} AND current_setting('app.actor_type', true) IS DISTINCT FROM 'USER' THEN
    {_fail("quotes_answer_by_person", "quotes: only a person records an answer")}
  END IF;
  IF OLD.status <> 'EXPIRED' AND NEW.status = 'EXPIRED' AND NOT NEW.valid_until <= now() THEN
    {_fail("quotes_not_expired", "quotes: the validity is not over")}
  END IF;
"""  # noqa: S608
ANCHOR = "  SELECT EXISTS (SELECT 1 FROM quote_approvals a WHERE"  # una vez en la de `0005`


def _guard() -> str:
    text: str = PREVIOUS.GUARD
    if text.count(ANCHOR) != 1:
        raise RuntimeError("quotes 0006: the 0005 guard changed; the anchor is not there")
    return text.replace(ANCHOR, BLOCK + ANCHOR)


GUARD = _guard()
body = PREVIOUS.body


def _scalar(schema_editor: Any, sql: str) -> Any:
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(sql)
        return cursor.fetchone()[0]


def _sees_all(schema_editor: Any) -> None:
    """Con FORCE RLS, un rol sin `BYPASSRLS` no ve filas: una comprobación vacía mentiría."""
    if not _scalar(schema_editor, "SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user"):
        raise RuntimeError("quotes 0006: run as crm_migrator with BYPASSRLS (RLS hides rows)")


def _refuse_legacy(apps: Any, schema_editor: Any) -> None:
    _sees_all(schema_editor)
    count = _scalar(
        schema_editor, "SELECT count(*) FROM public.quotes WHERE status IN ('ACCEPTED', 'REJECTED')"
    )
    if count:
        raise RuntimeError(
            f"quotes 0006: {count} accepted or rejected quotes have no answer date;"
            " resolve them first (docs/phases/phase-5.md, F5-09c3)"
        )


def _swap(schema_editor: Any, installed: str, wanted: str) -> None:
    if PREVIOUS._installed(schema_editor) != body(installed):
        raise RuntimeError("quotes 0006: the installed quotes_guard is not the expected one")
    schema_editor.execute(wanted.replace("__APP__", settings.DB_APP_ROLE))


def _replace(apps: Any, schema_editor: Any) -> None:
    _swap(schema_editor, PREVIOUS.GUARD, GUARD)


def _restore(apps: Any, schema_editor: Any) -> None:
    _swap(schema_editor, GUARD, PREVIOUS.GUARD)  # exactamente la de `0005`


def _refuse_answered(apps: Any, schema_editor: Any) -> None:
    """Volver atrás quitaría las fechas: no se pierde en silencio una respuesta del cliente."""
    _sees_all(schema_editor)
    where = " OR ".join(f"{c} IS NOT NULL" for c in COLUMNS)
    if _scalar(schema_editor, f"SELECT EXISTS (SELECT 1 FROM public.quotes WHERE {where})"):  # noqa: S608
        raise RuntimeError("quotes 0006: some quotes were answered; refusing to go back")


class Migration(migrations.Migration):
    dependencies = [("quotes", "0005_issue")]

    operations = [
        migrations.AddField(
            model_name="quote",
            name="accepted_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="quote",
            name="rejected_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="quote",
            name="rejection_reason",
            field=models.CharField(blank=True, max_length=500, null=True),
        ),
        migrations.RunPython(_refuse_legacy, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="quote",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("accepted_at__gte", models.F("sent_at")),
                        ("accepted_at__isnull", False),
                        ("accepted_at__lt", models.F("valid_until")),
                        ("status", "ACCEPTED"),
                    ),
                    models.Q(
                        models.Q(("status", "ACCEPTED"), _negated=True),
                        ("accepted_at__isnull", True),
                    ),
                    _connector="OR",
                ),
                name="quotes_accepted_ck",
            ),
        ),
        migrations.AddConstraint(
            model_name="quote",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("rejected_at__gte", models.F("sent_at")),
                        ("rejected_at__isnull", False),
                        ("rejected_at__lt", models.F("valid_until")),
                        ("rejection_reason__isnull", False),
                        ("rejection_reason__regex", "\\S"),
                        ("status", "REJECTED"),
                    ),
                    models.Q(
                        models.Q(("status", "REJECTED"), _negated=True),
                        ("rejected_at__isnull", True),
                        ("rejection_reason__isnull", True),
                    ),
                    _connector="OR",
                ),
                name="quotes_rejected_ck",
            ),
        ),
        migrations.RunPython(_replace, _restore),
        migrations.RunPython(migrations.RunPython.noop, _refuse_answered),  # al volver, lo primero
    ]
