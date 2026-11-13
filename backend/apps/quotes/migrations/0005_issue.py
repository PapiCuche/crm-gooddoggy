"""F5-09c2: la cotización emitida lleva sus fotografías (empresa, cliente, condiciones y garantía) y
su fecha de envío, y la base protege lo emitido. Un `CHECK` (para todo rol) exige que lo emitido
(`SENT` en adelante) lleve fotografías, envío y una validez posterior a él, y que lo no emitido no
lleve nada de eso. El disparador de `quotes` se reemplaza con `CREATE OR REPLACE` (`0003` ya está
fusionada) **añadiendo bloques a su cuerpo vigente**, que antes se compara con el de `0003` (si no
es ese, no se toca nada), en puntos que han de aparecer una sola vez. Para `crm_app`:

- una cotización nace sin fotografías ni envío;
- las fotografías y `sent_at` solo se escriben al pasar a `SENT`, y la validez ha de ser futura
  (el `now()` de la transacción);
- desde `PENDING_APPROVAL`, ni `notes_to_customer` ni `valid_until` cambian, salvo la validez al
  pasar de `APPROVED` a `SENT`; `internal_notes` sigue editable (no llega al cliente ni cambia el
  importe: decisión del propietario, ADR-031 pregunta 2);
- `cancelled_at` y `cancel_reason` no se reescriben en una cancelada (fuera de `CANCELLED`, el
  `CHECK` de `0001` ya los exige nulos: solo se escriben al entrar);
- lo de la IA (sin autor) no pasa de `DRAFT` a `APPROVED` ni a `SENT`, ni de `APPROVED` a `SENT`
  sin la última solicitud aprobada.

Todas las comparaciones de columnas que admiten nulos son `IS DISTINCT FROM`.

**Qué no hace:** no comprueba `quotes.send`, el alcance del contacto ni el suelo de costo (los
hace `issue_quote`); `app.user_id` y `app.actor_type` son variables de la transacción que fija
`tenant_scope`: un SQL hostil de `crm_app` podría fijarlas.

**Datos que ya existen.** Lo emitido antes de esta migración no tiene fotografías que inventar: si
hay alguna cotización en `SENT` o después, la migración se niega con un error claro (hay que
cancelarlas o resolverlas antes). Sin ellas el `CHECK` se valida al crearse (bloqueo
`ACCESS EXCLUSIVE` de `quotes` mientras la recorre; todas las demás filas lo cumplen porque las
columnas nuevas nacen nulas). Volver atrás se niega si alguna cotización tiene datos de emisión; si
no, restaura exactamente la función de `0003` y quita el `CHECK` y las columnas."""

from importlib import import_module
from typing import Any

from django.conf import settings
from django.db import migrations, models

PREVIOUS = import_module("apps.quotes.migrations.0003_quotes_guard_limit")
_fail = PREVIOUS._fail
COLUMNS = (
    "company_snapshot",
    "customer_snapshot",
    "terms_snapshot",
    "warranty_snapshot",
    "sent_at",
)
ISSUED = ["SENT", "VIEWED", "ACCEPTED", "REJECTED", "EXPIRED"]
UNISSUED = ["DRAFT", "PENDING_APPROVAL", "APPROVED"]
TO_SENT = "OLD.status <> 'SENT' AND NEW.status = 'SENT'"
LATEST_APPROVED = """    SELECT a.status = 'APPROVED' INTO approved
      FROM quote_approvals a  -- la última solicitud
     WHERE a.organization_id = NEW.organization_id AND a.quote_id = NEW.id
     ORDER BY a.created_at DESC, a.id DESC LIMIT 1;"""
BORN = f"""    IF {" OR ".join(f"NEW.{c} IS NOT NULL" for c in COLUMNS)} THEN
      {_fail("quotes_born_clean", "quotes: a quote is born without snapshots")}
    END IF;
"""  # noqa: S608
FROZEN = f"""  IF ({" OR ".join(f"NEW.{c} IS DISTINCT FROM OLD.{c}" for c in COLUMNS)})
     AND NOT ({TO_SENT}) THEN
    {_fail("quotes_issued_frozen", "quotes: the snapshots are written only when issuing")}
  END IF;
  IF {TO_SENT} AND NOT NEW.valid_until > now() THEN
    {_fail("quotes_valid_future", "quotes: an issued quote is valid until a future date")}
  END IF;
  IF OLD.status = 'CANCELLED' AND (NEW.cancelled_at IS DISTINCT FROM OLD.cancelled_at
     OR NEW.cancel_reason IS DISTINCT FROM OLD.cancel_reason) THEN  -- fuera, el CHECK los anula
    {_fail("quotes_cancel_frozen", "quotes: a cancellation is never rewritten")}
  END IF;
  IF OLD.status <> 'DRAFT' AND (NEW.notes_to_customer IS DISTINCT FROM OLD.notes_to_customer
     OR (NEW.valid_until IS DISTINCT FROM OLD.valid_until
         AND NOT (OLD.status = 'APPROVED' AND NEW.status = 'SENT'))) THEN
    {_fail("quotes_terms_frozen", "quotes: what the customer reads is frozen after DRAFT")}
  END IF;
"""  # noqa: S608
AI = f"""  IF NEW.created_by_user_id IS NULL AND OLD.status = 'DRAFT'
     AND NEW.status IN ('APPROVED', 'SENT') THEN  -- la IA no sale sola: pasa por la solicitud
    {_fail("quotes_ai_needs_approval", "quotes: what the AI wrote needs an approved request")}
  END IF;
  IF NEW.created_by_user_id IS NULL AND OLD.status = 'APPROVED' AND NEW.status = 'SENT' THEN
{LATEST_APPROVED}
    IF approved IS NOT TRUE THEN
      {_fail("quotes_ai_needs_approval", "quotes: what the AI wrote needs an approved request")}
    END IF;
  END IF;
"""  # noqa: S608
ANCHORS = (  # cada bloque va justo antes de su ancla, que aparece una sola vez en la de `0003`
    ("    RETURN NEW;\n  END IF;\n  IF NEW.organization_id <> OLD.organization_id", BORN),
    ("  SELECT EXISTS (SELECT 1 FROM quote_approvals a WHERE", FROZEN),
    ("  IF OLD.status = 'DRAFT' AND NEW.status IN ('APPROVED', 'SENT') THEN", AI),
)


def _guard() -> str:
    text: str = PREVIOUS.QUOTES_GUARD
    for anchor, block in ANCHORS:
        if text.count(anchor) != 1:
            raise RuntimeError("quotes 0005: the 0003 guard changed; the anchors are not there")
        text = text.replace(anchor, block + anchor)
    return text


GUARD = _guard()


def body(sql: str) -> str:
    """El cuerpo de la función (entre `$f$`), como lo guarda PostgreSQL en `pg_proc.prosrc`."""
    return sql.replace("__APP__", settings.DB_APP_ROLE).split("$f$")[1]


def _installed(schema_editor: Any) -> str:
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            "SELECT prosrc FROM pg_proc WHERE oid = 'public.quotes_guard()'::regprocedure"
        )
        return str(cursor.fetchone()[0])


def _exists(schema_editor: Any, condition: str) -> bool:
    with schema_editor.connection.cursor() as cursor:  # `crm_migrator`: BYPASSRLS, ve todas
        cursor.execute(f"SELECT EXISTS (SELECT 1 FROM public.quotes WHERE {condition})")  # noqa: S608
        return bool(cursor.fetchone()[0])


def _refuse_legacy(apps: Any, schema_editor: Any) -> None:
    if _exists(schema_editor, "status IN ({})".format(",".join(f"'{s}'" for s in ISSUED))):
        raise RuntimeError(
            "quotes 0005: some quotes were issued before snapshots existed; resolve them first"
        )


def _replace(apps: Any, schema_editor: Any) -> None:
    if _installed(schema_editor) != body(PREVIOUS.QUOTES_GUARD):
        raise RuntimeError("quotes 0005: the installed quotes_guard is not the one of 0003")
    schema_editor.execute(GUARD.replace("__APP__", settings.DB_APP_ROLE))


def _restore(apps: Any, schema_editor: Any) -> None:
    PREVIOUS._replace(apps, schema_editor)  # exactamente la de `0003`


def _refuse_issued(apps: Any, schema_editor: Any) -> None:
    """Volver atrás quitaría las fotografías: no se pierde en silencio lo que ya se emitió."""
    if _exists(schema_editor, " OR ".join(f"{c} IS NOT NULL" for c in COLUMNS)):
        raise RuntimeError("quotes 0005: some quotes were issued; refusing to go back")


class Migration(migrations.Migration):
    dependencies = [("quotes", "0004_approval_submitter")]

    operations = [
        migrations.AddField(
            model_name="quote",
            name="company_snapshot",
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="quote",
            name="customer_snapshot",
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="quote",
            name="sent_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="quote",
            name="terms_snapshot",
            field=models.CharField(blank=True, max_length=2000, null=True),
        ),
        migrations.AddField(
            model_name="quote",
            name="warranty_snapshot",
            field=models.CharField(blank=True, max_length=500, null=True),
        ),
        migrations.RunPython(_refuse_legacy, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="quote",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        models.Q(("status__in", ISSUED), _negated=True),
                        models.Q(
                            ("company_snapshot__isnull", False),
                            ("customer_snapshot__isnull", False),
                            ("sent_at__isnull", False),
                            ("valid_until__gt", models.F("sent_at")),
                            ("valid_until__isnull", False),
                        ),
                        _connector="OR",
                    ),
                    models.Q(
                        models.Q(("status__in", UNISSUED), _negated=True),
                        models.Q(
                            ("company_snapshot__isnull", True),
                            ("customer_snapshot__isnull", True),
                            ("sent_at__isnull", True),
                            ("terms_snapshot__isnull", True),
                            ("warranty_snapshot__isnull", True),
                        ),
                        _connector="OR",
                    ),
                ),
                name="quotes_issued_ck",
            ),
        ),
        migrations.RunPython(_replace, _restore),
        migrations.RunPython(migrations.RunPython.noop, _refuse_issued),  # al volver, lo primero
    ]
