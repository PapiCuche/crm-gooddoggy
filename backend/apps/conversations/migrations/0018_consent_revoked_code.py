"""F7-08b: `CONSENT_REVOKED` entre los motivos de un `FAILED` (`messages.provider_error_code`): un
envío proactivo que esperaba en la cola cuando el cliente se dio de baja. Hacia atrás falla si ya
hay mensajes con ese motivo (la base no los deja con un código que no conoce)."""

from django.db import migrations, models

BEFORE = (
    "CONVERSATION_CLOSED", "CHANNEL_UNAVAILABLE", "CONTACT_BLOCKED", "WINDOW_CLOSED",
    "ADAPTER_REJECTED", "RETRIES_EXHAUSTED", "TOKEN_INVALID", "RECIPIENT_UNREACHABLE",
    "PROVIDER_REJECTED", "PROVIDER_UNKNOWN", "RATE_LIMITED", "QUALITY_BLOCKED",
    "TEMPLATE_UNAVAILABLE", "PROVIDER_ERROR",
)  # fmt: skip
AFTER = (*BEFORE[:4], "CONSENT_REVOKED", *BEFORE[4:])
NAME = "messages_error_code_ck"


def check(codes: tuple[str, ...]) -> models.CheckConstraint:
    return models.CheckConstraint(
        condition=models.Q(("provider_error_code__isnull", True))
        | models.Q(("delivery_status", "FAILED"), ("provider_error_code__in", codes)),
        name=NAME,
    )


class Migration(migrations.Migration):
    dependencies = [
        ("conversations", "0017_channel_health_due"),
    ]

    operations = [
        migrations.RemoveConstraint(model_name="message", name=NAME),
        migrations.AddConstraint(model_name="message", constraint=check(AFTER)),
    ]
