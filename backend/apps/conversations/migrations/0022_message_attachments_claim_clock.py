"""F7-09a1c: el disparador `message_attachments_advance` (`0021`) también ata el reclamo al reloj de
la base: pasar de `PENDING` a `DOWNLOADING` antes de `not_before` (la espera de un reintento) y
reclamar un `DOWNLOADING` con un `attempt_id` nuevo antes de que venza su `lease_until` (el plazo
del intento vivo) son `check_violation`. `now()` es el instante de inicio de la transacción, el
mismo que usa `media.claim` (`Now()`) para elegir la fila.

La función nueva se construye a partir del cuerpo de `0021` con un ancla que aparece una sola vez,
y antes de reemplazarla se compara con la que hay en la base: si no es exactamente la de `0021`
(o, hacia atrás, la de aquí), la migración falla (`RuntimeError`) sin tocar nada. Hacia atrás
vuelve el cuerpo exacto de `0021`. Solo cambia el cuerpo (`CREATE OR REPLACE`): el disparador, el
dueño, `search_path` y los privilegios siguen como estaban."""

import importlib
from typing import Any

from django.db import migrations

ADVANCE = importlib.import_module("apps.conversations.migrations.0021_message_attachments").ADVANCE
HEAD, OLD, _ = ADVANCE.split("$fn$")
ANCHOR = (
    "        AND (OLD.download_status = 'PENDING' "
    "OR NEW.attempt_id IS DISTINCT FROM OLD.attempt_id);\n"
)
CLOCK = """    -- 0022, con el reloj de la base: ni antes de la espera ni sobre un plazo vivo.
    IF claim AND ((OLD.download_status = 'PENDING' AND OLD.not_before IS NOT NULL
                   AND OLD.not_before > now())
                  OR (OLD.download_status = 'DOWNLOADING' AND OLD.lease_until > now())) THEN
        RAISE EXCEPTION 'message_attachments_advance: reclamo antes de tiempo'
            USING ERRCODE = 'check_violation';
    END IF;
"""
if OLD.count(ANCHOR) != 1 or HEAD.count("CREATE FUNCTION") != 1:
    raise RuntimeError("0022: el cuerpo de 0021 no es el esperado")
NEW = OLD.replace(ANCHOR, ANCHOR + CLOCK)
REPLACE = HEAD.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION")


def _swap(expected: str, body: str) -> Any:
    def run(apps: Any, schema_editor: Any) -> None:
        with schema_editor.connection.cursor() as cursor:
            cursor.execute(
                "SELECT prosrc FROM pg_proc WHERE oid = "
                "'public.message_attachments_advance()'::regprocedure"
            )
            (live,) = cursor.fetchone()
            if live != expected:
                raise RuntimeError("message_attachments_advance no es la versión esperada")
            cursor.execute(f"{REPLACE}$fn${body}$fn$")

    return run


class Migration(migrations.Migration):
    dependencies = [
        ("conversations", "0021_message_attachments"),
    ]

    operations = [
        migrations.RunPython(_swap(OLD, NEW), _swap(NEW, OLD)),
    ]
