"""F2-80 (ADR-020 §2): las invitaciones no se borran. Revocar es un estado, y los topes de una
organización se cuentan sobre la tabla: el runtime pierde `DELETE` sobre ella."""

from typing import Any

from django.conf import settings
from django.db import migrations


def _privilege(verb: str) -> Any:
    def run(apps: Any, schema_editor: Any) -> None:
        app_role = schema_editor.quote_name(settings.DB_APP_ROLE)
        word = "FROM" if verb == "REVOKE" else "TO"
        schema_editor.execute(f"{verb} DELETE ON public.user_invitations {word} {app_role}")

    return run


class Migration(migrations.Migration):
    dependencies = [("organizations", "0009_user_invitations")]

    operations = [migrations.RunPython(_privilege("REVOKE"), _privilege("GRANT"))]
