"""F2-130g (ADR-020 §3): lo enviado de una invitación no se deshace. `send_count` no baja,
`send_times` solo crece por el final (la de antes es prefijo de la nueva) y `sent_at`, una vez
puesto, ni vuelve a NULL ni se adelanta; enviada una vez, la invitación no cambia de correo ni de
organización (el tope diario cuenta por correo). Los topes de envío (5 por invitación, 3 por correo
en 24 horas) dejan de depender solo del código. Disparador para todos los roles, también el
propietario de la tabla; la función es INVOKER (no `SECURITY DEFINER`) y solo compara la fila.
Renovar, revocar y aceptar no tocan esas columnas; enviar suma, añade y pone `sent_at = now()`."""

from django.db import migrations

GUARD = """
CREATE FUNCTION user_invitations_sends_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $fn$
BEGIN
    IF NEW.send_count < OLD.send_count
       OR NEW.send_times[1:cardinality(OLD.send_times)] IS DISTINCT FROM OLD.send_times
       OR array_lower(NEW.send_times, 1) <> 1
       OR OLD.sent_at IS NOT NULL AND (NEW.sent_at IS NULL OR NEW.sent_at < OLD.sent_at)
       OR OLD.send_count > 0 AND (NEW.email, NEW.organization_id)
          IS DISTINCT FROM (OLD.email, OLD.organization_id) THEN
        RAISE EXCEPTION 'user_invitations_sends_guard: lo enviado de una invitación no se deshace'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$fn$;
CREATE TRIGGER user_invitations_sends_guard BEFORE UPDATE ON user_invitations
    FOR EACH ROW EXECUTE FUNCTION user_invitations_sends_guard();
"""
UNGUARD = """
DROP TRIGGER IF EXISTS user_invitations_sends_guard ON user_invitations;
DROP FUNCTION IF EXISTS user_invitations_sends_guard();
"""


class Migration(migrations.Migration):
    dependencies = [("organizations", "0012_invitation_delivery")]

    operations = [migrations.RunSQL(GUARD, UNGUARD)]
