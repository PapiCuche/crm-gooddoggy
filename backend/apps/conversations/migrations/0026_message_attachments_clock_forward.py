"""F7-09a1d: el disparador `message_attachments_advance` (`0021`, `0022`) tampoco deja adelantar el
reloj fuera de un reclamo. Un UPDATE que no es un reclamo no mueve `lease_until` (el plazo de un
intento) ni `not_before` (la espera de un reintento) a un instante anterior ni a NULL; un reintento
(`DOWNLOADING` → `PENDING`) pone una espera no anterior a `statement_timestamp()`. Si no, adelantar
el plazo y reclamar después (dos sentencias) burlaría la regla de `0022`. Es `check_violation`.

Ata a **todos** los roles, como `0021`: también al migrador. Quien tenga que mover el reloj a mano
(solo las pruebas lo hacen) es el dueño de la tabla, que apaga el disparador a la vista dentro de
su propia transacción; `crm_app` no puede. La base sigue sin atar un reintento al intento que
tiene el plazo: eso lo hace la aplicación (`media._mine`).

La función nueva sale del cuerpo de `0022` con un ancla que aparece una sola vez, y antes de
reemplazarla se compara con la que hay en la base (`0022._swap`): si no es exactamente la esperada,
falla sin tocar nada. Hacia atrás vuelve el cuerpo exacto de `0022`.

Además, el índice parcial `(organization_id, updated_at)` de las filas abiertas (`PENDING` y
`DOWNLOADING`) para el barredor (`media.stranded`); fuera del estado de Django, como el de
`0021`. Se crea sin `CONCURRENTLY` (la tabla es de F7-09a1b y aún pequeña)."""

import importlib

from django.db import migrations

CLOCK = importlib.import_module(
    "apps.conversations.migrations.0022_message_attachments_claim_clock"
)
ANCHOR = CLOCK.CLOCK
FORWARD = """    -- 0026: fuera de un reclamo, ni el plazo ni la espera vencen antes.
    IF NOT claim AND (
        (OLD.lease_until IS NOT NULL
         AND (NEW.lease_until IS NULL OR NEW.lease_until < OLD.lease_until))
        OR (OLD.download_status = 'DOWNLOADING' AND NEW.download_status = 'PENDING'
            AND (NEW.not_before IS NULL OR NEW.not_before < statement_timestamp()))
        OR (NOT (OLD.download_status = 'DOWNLOADING' AND NEW.download_status = 'PENDING')
            AND OLD.not_before IS NOT NULL
            AND (NEW.not_before IS NULL OR NEW.not_before < OLD.not_before))
    ) THEN
        RAISE EXCEPTION 'message_attachments_advance: el reloj no se adelanta'
            USING ERRCODE = 'check_violation';
    END IF;
"""
if CLOCK.NEW.count(ANCHOR) != 1:
    raise RuntimeError("0026: el cuerpo de 0022 no es el esperado")
NEW = CLOCK.NEW.replace(ANCHOR, ANCHOR + FORWARD)
SWEEP_INDEX = (
    "CREATE INDEX message_attachments_sweep_idx ON public.message_attachments "
    "(organization_id, updated_at) WHERE download_status IN ('PENDING', 'DOWNLOADING')"
)


class Migration(migrations.Migration):
    dependencies = [
        ("conversations", "0025_validate_media_message_types"),
    ]

    operations = [
        migrations.RunPython(CLOCK._swap(CLOCK.NEW, NEW), CLOCK._swap(NEW, CLOCK.NEW)),
        migrations.RunSQL(SWEEP_INDEX, "DROP INDEX IF EXISTS public.message_attachments_sweep_idx"),
    ]
