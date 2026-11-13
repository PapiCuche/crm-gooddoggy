"""F5-09c4: revisar una cotización crea la revisión siguiente del mismo número y cancela la anterior
en la misma transacción; la base lo protege.

- Un índice único parcial (para todo rol): de cada número, a lo sumo **una** revisión que no está
  `CANCELLED` (`quotes_one_live_uq`). Antes de crearlo, si ya hubiera números con más de una, la
  migración se niega y dice cuántos (lo comprueba `crm_migrator`, que ha de tener `BYPASSRLS`: bajo
  RLS no las vería).
- El disparador de `quotes` se reemplaza con `CREATE OR REPLACE` (`0006` ya está fusionada)
  **añadiendo un bloque a su cuerpo vigente**, que antes se compara con el de `0006` (si no es ese,
  no se toca nada), en un punto que ha de aparecer una sola vez (la rama del `INSERT`). Para
  `crm_app`, una revisión `> 1` solo nace si la anterior del mismo número (`revision - 1`) quedó
  `CANCELLED` **en esta misma transacción** (`cancelled_at = now()`: el `now()` de la base es el
  de la transacción, y el comando fecha la cancelación con él) y es del mismo contacto, oportunidad,
  lista de precios y autor (`IS NOT DISTINCT FROM`: lo de la IA, sin autor, sigue sin autor)
  (`quotes_revision_chain`).

**Qué no hace:** no comprueba `quotes.*` ni su alcance (los comandos); un SQL de `crm_app` puede
cancelar una cotización abierta e insertar él mismo la revisión siguiente válida (nace en `DRAFT`,
sin fotografías, con los disparadores de siempre). Lo terminal sigue terminal: el grafo de estados
no cambia. Volver atrás restaura exactamente la función de `0006` (tras comparar la instalada) y
quita el índice; las revisiones que existan siguen siendo válidas para `0006`."""

from importlib import import_module
from typing import Any

from django.conf import settings
from django.db import migrations, models

PREVIOUS = import_module("apps.quotes.migrations.0006_answer")
_fail = PREVIOUS._fail
BLOCK = f"""    IF NEW.revision > 1 AND NOT EXISTS (
        SELECT 1 FROM public.quotes p
         WHERE p.organization_id = NEW.organization_id AND p.number = NEW.number
           AND p.revision = NEW.revision - 1 AND p.status = 'CANCELLED'
           AND p.cancelled_at = now() AND p.contact_id = NEW.contact_id
           AND p.price_list_id = NEW.price_list_id
           AND p.opportunity_id IS NOT DISTINCT FROM NEW.opportunity_id
           AND p.created_by_user_id IS NOT DISTINCT FROM NEW.created_by_user_id) THEN
      {_fail("quotes_revision_chain", "quotes: a revision follows the one it cancels")}
    END IF;
"""  # noqa: S608
ANCHOR = "    RETURN NEW;\n  END IF;\n  IF NEW.organization_id <> OLD.organization_id"  # una vez


def _guard() -> str:
    text: str = PREVIOUS.GUARD
    if text.count(ANCHOR) != 1:
        raise RuntimeError("quotes 0007: the 0006 guard changed; the anchor is not there")
    return text.replace(ANCHOR, BLOCK + ANCHOR)


GUARD = _guard()
body = PREVIOUS.body


def _swap(schema_editor: Any, installed: str, wanted: str) -> None:
    if PREVIOUS.PREVIOUS._installed(schema_editor) != body(installed):
        raise RuntimeError("quotes 0007: the installed quotes_guard is not the expected one")
    schema_editor.execute(wanted.replace("__APP__", settings.DB_APP_ROLE))


def _replace(apps: Any, schema_editor: Any) -> None:
    _swap(schema_editor, PREVIOUS.GUARD, GUARD)


def _restore(apps: Any, schema_editor: Any) -> None:
    _swap(schema_editor, GUARD, PREVIOUS.GUARD)  # exactamente la de `0006`


def _refuse_duplicates(apps: Any, schema_editor: Any) -> None:
    """Un número con dos revisiones vivas no tiene una que elegir: hay que resolverlo antes."""
    PREVIOUS._sees_all(schema_editor)
    count = PREVIOUS._scalar(
        schema_editor,
        "SELECT count(*) FROM (SELECT 1 FROM public.quotes WHERE status <> 'CANCELLED'"
        " GROUP BY organization_id, number HAVING count(*) > 1) d",
    )
    if count:
        raise RuntimeError(
            f"quotes 0007: {count} quote numbers have more than one live revision;"
            " cancel the extra ones first (docs/phases/phase-5.md, F5-09c4)"
        )


class Migration(migrations.Migration):
    dependencies = [("quotes", "0006_answer")]

    operations = [
        migrations.RunPython(_refuse_duplicates, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="quote",
            constraint=models.UniqueConstraint(
                condition=models.Q(("status", "CANCELLED"), _negated=True),
                fields=("organization_id", "number"),
                name="quotes_one_live_uq",
            ),
        ),
        migrations.RunPython(_replace, _restore),
    ]
