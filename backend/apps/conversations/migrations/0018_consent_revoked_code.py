"""F7-08b: `CONSENT_REVOKED` entre los motivos de un `FAILED` (`messages.provider_error_code`): un
envío proactivo que esperaba en la cola cuando el cliente se dio de baja.

El `CHECK` nuevo se añade `NOT VALID` (sin recorrer `messages` bajo un bloqueo exclusivo: con
volumen de producción un `ADD CHECK` normal bloquearía la tabla) y rige ya para toda fila nueva o
modificada; la `0019` lo valida (bloqueo que no impide leer ni escribir). Hacia atrás vuelve el
`CHECK` anterior, que recorre la tabla y falla si ya hay mensajes con ese motivo."""

from django.db import migrations, models

BEFORE = (
    "CONVERSATION_CLOSED", "CHANNEL_UNAVAILABLE", "CONTACT_BLOCKED", "WINDOW_CLOSED",
    "ADAPTER_REJECTED", "RETRIES_EXHAUSTED", "TOKEN_INVALID", "RECIPIENT_UNREACHABLE",
    "PROVIDER_REJECTED", "PROVIDER_UNKNOWN", "RATE_LIMITED", "QUALITY_BLOCKED",
    "TEMPLATE_UNAVAILABLE", "PROVIDER_ERROR",
)  # fmt: skip
AFTER = (*BEFORE[:4], "CONSENT_REVOKED", *BEFORE[4:])
NAME = "messages_error_code_ck"
TABLE = "public.messages"


def check(codes: tuple[str, ...]) -> models.CheckConstraint:
    return models.CheckConstraint(
        condition=models.Q(("provider_error_code__isnull", True))
        | models.Q(("delivery_status", "FAILED"), ("provider_error_code__in", codes)),
        name=NAME,
    )


def sql(codes: tuple[str, ...], tail: str) -> str:
    listed = ", ".join(f"'{c}'" for c in codes)
    return (
        f"ALTER TABLE {TABLE} ADD CONSTRAINT {NAME} CHECK (provider_error_code IS NULL OR "
        f"(delivery_status = 'FAILED' AND provider_error_code IN ({listed}))){tail}"
    )


class Migration(migrations.Migration):
    dependencies = [
        ("conversations", "0017_channel_health_due"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RemoveConstraint(model_name="message", name=NAME),
                migrations.AddConstraint(model_name="message", constraint=check(AFTER)),
            ],
            database_operations=[
                migrations.RunSQL(
                    [f"ALTER TABLE {TABLE} DROP CONSTRAINT {NAME}", sql(AFTER, " NOT VALID")],
                    [f"ALTER TABLE {TABLE} DROP CONSTRAINT {NAME}", sql(BEFORE, "")],
                ),
            ],
        ),
    ]
