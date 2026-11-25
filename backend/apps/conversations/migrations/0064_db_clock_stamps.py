"""F7b-21: las tres horas que comparan `delivery._sent_with` las pone el reloj de PostgreSQL.

`sending_at` (el reclamo, con la cuenta en `FOR SHARE`), `revoked_at` y `created_at` (rotar o
desconectar, con la cuenta en `FOR NO KEY UPDATE`) se escribían con la hora de cada proceso: con
web y worker en hosts distintos, o un reloj que NTP atrasa, el orden de las horas no era el de los
bloqueos y un `190` tardío del token viejo podía degradar la credencial nueva. Tres disparadores
`BEFORE` (INVOKER, para todo rol) sustituyen el valor por `clock_timestamp()`, la hora de la base
en ese instante, después de los bloqueos y antes del commit:
- `messages_sending_stamped`: solo al pasar a `SENDING` (el reclamo).
- `credentials_created_stamped`: al insertar.
- `credentials_revoked_stamped`: solo de `NULL` a una hora (la invalidación).
Solo reescriben un valor, nunca rechazan: aceptan toda escritura de la imagen anterior, que durante
el despliegue y en una vuelta atrás sigue mandando la hora de su proceso (expandir, ADR-033 §7).
Ningún disparador hace consultas ni bloquea. No lo cubren: reescribir a mano `sending_at` de un
`SENDING`, o un `revoked_at` ya puesto (arreglos de datos), ni las filas anteriores a esta
migración. Hacia atrás se borran los tres disparadores y las dos funciones."""

from django.db import migrations

STAMP = """
SET LOCAL lock_timeout = '5s';
CREATE FUNCTION public.messages_sending_stamped() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $fn$
BEGIN
    NEW.sending_at := pg_catalog.clock_timestamp();
    RETURN NEW;
END
$fn$;
REVOKE ALL ON FUNCTION public.messages_sending_stamped() FROM PUBLIC;
CREATE TRIGGER messages_sending_stamped
    BEFORE UPDATE OF delivery_status ON public.messages
    FOR EACH ROW WHEN (
        NEW.delivery_status = 'SENDING' AND OLD.delivery_status IS DISTINCT FROM 'SENDING'
    )
    EXECUTE FUNCTION public.messages_sending_stamped();
CREATE FUNCTION public.credentials_stamped() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $fn$
BEGIN
    IF TG_OP = 'INSERT' THEN
        NEW.created_at := pg_catalog.clock_timestamp();
    ELSE
        NEW.revoked_at := pg_catalog.clock_timestamp();
    END IF;
    RETURN NEW;
END
$fn$;
REVOKE ALL ON FUNCTION public.credentials_stamped() FROM PUBLIC;
CREATE TRIGGER credentials_created_stamped
    BEFORE INSERT ON public.credentials
    FOR EACH ROW EXECUTE FUNCTION public.credentials_stamped();
CREATE TRIGGER credentials_revoked_stamped
    BEFORE UPDATE OF revoked_at ON public.credentials
    FOR EACH ROW WHEN (OLD.revoked_at IS NULL AND NEW.revoked_at IS NOT NULL)
    EXECUTE FUNCTION public.credentials_stamped();
"""
UNSTAMP = """
SET LOCAL lock_timeout = '5s';
DROP TRIGGER messages_sending_stamped ON public.messages;
DROP FUNCTION public.messages_sending_stamped();
DROP TRIGGER credentials_created_stamped ON public.credentials;
DROP TRIGGER credentials_revoked_stamped ON public.credentials;
DROP FUNCTION public.credentials_stamped();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("conversations", "0063_conversations_unreplied_index"),
    ]

    operations = [migrations.RunSQL(STAMP, UNSTAMP)]
