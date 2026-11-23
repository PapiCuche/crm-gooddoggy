"""F7b-15: un `FAILED` solo cambia su motivo una vez, de `PROVIDER_UNKNOWN` a uno definitivo.

El barredor de `SENDING` cierra con `PROVIDER_UNKNOWN` lo que no se supo si salió; si Meta dice
después `failed`, `advance_delivery` guarda su motivo, su código y su hora sin cambiar el estado. El
disparador `messages_delivery_advance` solo mira los cambios de estado, así que este otro guarda lo
demás: con el estado `FAILED` antes y después, `provider_error_code`, `provider_error_detail` y
`failed_at` solo cambian si el motivo era `PROVIDER_UNKNOWN` y pasa a otro no nulo. Un motivo
definitivo no se reescribe. Como el de entrega: sin `SECURITY DEFINER`, `search_path` fijo con
`pg_temp` al final; `lock_timeout` de 5 s en los dos sentidos (`messages` es grande). Hacia atrás
se borran el disparador y la función. Ata también a `crm_migrator`: un arreglo de datos de motivos,
detalles u horas de un `FAILED` tiene que quitarlo y recrearlo en una migración revisada y listada
en `DROPS` (`tests/test_message_media.py`)."""

from django.db import migrations

SETTLED = """
SET LOCAL lock_timeout = '5s';
CREATE FUNCTION public.messages_failure_settled() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $fn$
BEGIN
    IF coalesce(OLD.provider_error_code, '') <> 'PROVIDER_UNKNOWN'
        OR coalesce(NEW.provider_error_code, 'PROVIDER_UNKNOWN') = 'PROVIDER_UNKNOWN' THEN
        RAISE EXCEPTION 'messages_failure_settled: el motivo de un FAILED solo se corrige una vez'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$fn$;
REVOKE ALL ON FUNCTION public.messages_failure_settled() FROM PUBLIC;
CREATE TRIGGER messages_failure_settled
    BEFORE UPDATE OF provider_error_code, provider_error_detail, failed_at ON public.messages
    FOR EACH ROW WHEN (
        OLD.delivery_status = 'FAILED' AND NEW.delivery_status = 'FAILED'
        AND (OLD.provider_error_code, OLD.provider_error_detail, OLD.failed_at)
            IS DISTINCT FROM (NEW.provider_error_code, NEW.provider_error_detail, NEW.failed_at)
    )
    EXECUTE FUNCTION public.messages_failure_settled();
"""
DROP = """
SET LOCAL lock_timeout = '5s';
DROP TRIGGER messages_failure_settled ON public.messages;
DROP FUNCTION public.messages_failure_settled();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("conversations", "0058_validate_error_detail"),
    ]

    operations = [migrations.RunSQL(SETTLED, DROP)]
