"""F2-130a (ADR-020 §3): encontrar la invitación de un enlace por el hash de su secreto. Es la única
lectura de `user_invitations` fuera de RLS, como `resolve_channel_account` (ADR-002 §3.3): recibe
el hash como texto y no devuelve nada si no tiene la forma de un SHA-256; no escribe; devuelve
solo la organización y el id, y solo de una invitación pendiente y sin caducar."""

from django.db import migrations

from core.db.operations import SecurityDefinerFunction


class Migration(migrations.Migration):
    dependencies = [("organizations", "0010_invitations_are_not_deleted")]

    operations = [
        SecurityDefinerFunction(
            "resolve_invitation",
            "p_token_hash text",
            "text",
            "TABLE (organization_id uuid, invitation_id uuid)",
            "SELECT i.organization_id, i.id FROM public.user_invitations i "
            "WHERE p_token_hash ~ '^[0-9a-f]{64}$' AND i.token_hash = p_token_hash "
            "AND i.status = 'PENDING' AND i.expires_at > now() LIMIT 1",
            search_path="pg_catalog, public, pg_temp",  # pg_temp al final (F3-52)
        ),
    ]
