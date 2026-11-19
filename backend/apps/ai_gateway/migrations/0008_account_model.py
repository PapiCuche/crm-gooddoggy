"""F8-05b6 (#965; ADR-034 §3, §4): el modelo de la cuenta del proveedor de IA. Las cuentas que ya
existen quedan con el modelo que el gateway usaba hasta hoy (`AI_PROVIDER_PARAMS`), congelado aquí:
su gasto no cambia. La base solo comprueba la forma del id: la lista permitida (`AI_MODELS`, que es
también la tabla de precios) la aplica la aplicación, y el gateway no llama con un modelo sin
precio. Un CHECK con la lista ataría cada cambio de la lista a una migración y no protegería más: lo
que gasta lo decide el precio. `crm_app` puede cambiar solo el modelo de una cuenta de su
organización (RLS con FORCE); `updated_at` ya lo tenía (0001). Sin función nueva."""

from typing import Any

from django.conf import settings
from django.db import migrations, models

TODAY = "claude-sonnet-5-5"  # el `AI_PROVIDER_PARAMS["ANTHROPIC"]["model"]` del 2026-11-19


def _grant(apps: Any, schema_editor: Any) -> None:
    role = schema_editor.quote_name(settings.DB_APP_ROLE)
    schema_editor.execute(f"GRANT UPDATE (model) ON public.ai_provider_accounts TO {role}")


def _revoke(apps: Any, schema_editor: Any) -> None:
    role = schema_editor.quote_name(settings.DB_APP_ROLE)
    schema_editor.execute(f"REVOKE UPDATE (model) ON public.ai_provider_accounts FROM {role}")


class Migration(migrations.Migration):
    dependencies = [("ai_gateway", "0007_orphan_reservations")]

    operations = [
        migrations.AddField(
            model_name="aiprovideraccount",
            name="model",
            field=models.CharField(default=TODAY, max_length=64),
            preserve_default=False,
        ),
        migrations.AddConstraint(
            model_name="aiprovideraccount",
            constraint=models.CheckConstraint(
                condition=models.Q(("model__regex", "^[a-z0-9][a-z0-9._-]*$")),
                name="ai_provider_accounts_model_ck",
            ),
        ),
        migrations.RunPython(_grant, _revoke),
    ]
