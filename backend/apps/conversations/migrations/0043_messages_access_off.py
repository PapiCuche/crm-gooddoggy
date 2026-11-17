"""F7b-05a (ADR-035 §3 y §6): `MESSAGES_ACCESS_OFF`, el titular de Instagram apagó «Allow access to
messages» (`200`/`2534041`), como motivo de un `FAILED` (`messages_error_code_ck`) y como detalle de
una cuenta en `ERROR` (`channel_accounts_detail_ck`).

`messages` es grande: su `CHECK` nuevo se añade `NOT VALID` con `lock_timeout` de 5 s (rige ya para
toda fila nueva o modificada) y la `0044` lo valida sin impedir leer ni escribir, como la
`0018`/`0019`. `channel_accounts` tiene pocas filas: su `CHECK` se cambia y se valida aquí.
Hacia atrás vuelven los anteriores, que fallan si ya hay un mensaje o una cuenta con el código
nuevo (no se pierde nada)."""

from django.db import migrations, models

MESSAGES, ACCOUNTS = "public.messages", "public.channel_accounts"
ERROR_CK, DETAIL_CK = "messages_error_code_ck", "channel_accounts_detail_ck"
NEW = "MESSAGES_ACCESS_OFF"
CODES = (
    "CONVERSATION_CLOSED", "CHANNEL_UNAVAILABLE", "CONTACT_BLOCKED", "WINDOW_CLOSED",
    "CONSENT_REVOKED", "ADAPTER_REJECTED", "RETRIES_EXHAUSTED", "TOKEN_INVALID",
    "RECIPIENT_UNREACHABLE", "PROVIDER_REJECTED", "PROVIDER_UNKNOWN", "RATE_LIMITED",
    "QUALITY_BLOCKED", "TEMPLATE_UNAVAILABLE", "PROVIDER_ERROR", "MEDIA_NOT_SCANNED",
)  # fmt: skip
DETAILS = (
    "TOKEN_INVALID", "WEBHOOK_NOT_SEEN", "QUALITY_LOW", "LIMIT_REACHED", "DISCONNECTED_BY_ADMIN",
    "PROVIDER_ERROR",
)  # fmt: skip
LOCK = "SET LOCAL lock_timeout = '5s'"


def error_ck(codes: tuple[str, ...]) -> models.CheckConstraint:
    return models.CheckConstraint(
        condition=models.Q(provider_error_code__isnull=True)
        | (models.Q(delivery_status="FAILED") & models.Q(provider_error_code__in=codes)),
        name=ERROR_CK,
    )


def detail_ck(details: tuple[str, ...]) -> models.CheckConstraint:
    return models.CheckConstraint(
        condition=models.Q(status_detail__isnull=True) | models.Q(status_detail__in=details),
        name=DETAIL_CK,
    )


def _in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def error_sql(codes: tuple[str, ...], tail: str) -> str:
    return (
        f"ALTER TABLE {MESSAGES} ADD CONSTRAINT {ERROR_CK} CHECK (provider_error_code IS NULL OR "
        f"(delivery_status = 'FAILED' AND provider_error_code IN ({_in(codes)}))){tail}"
    )


def detail_sql(details: tuple[str, ...]) -> str:
    return (
        f"ALTER TABLE {ACCOUNTS} ADD CONSTRAINT {DETAIL_CK} CHECK (status_detail IS NULL OR "
        f"status_detail IN ({_in(details)}))"
    )


class Migration(migrations.Migration):
    dependencies = [
        ("conversations", "0042_social_ingress"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RemoveConstraint(model_name="message", name=ERROR_CK),
                migrations.AddConstraint(model_name="message", constraint=error_ck((*CODES, NEW))),
                migrations.RemoveConstraint(model_name="channelaccount", name=DETAIL_CK),
                migrations.AddConstraint(
                    model_name="channelaccount", constraint=detail_ck((*DETAILS, NEW))
                ),
            ],
            database_operations=[
                migrations.RunSQL(
                    [
                        LOCK,
                        f"ALTER TABLE {MESSAGES} DROP CONSTRAINT {ERROR_CK}",
                        error_sql((*CODES, NEW), " NOT VALID"),
                        f"ALTER TABLE {ACCOUNTS} DROP CONSTRAINT {DETAIL_CK}",
                        detail_sql((*DETAILS, NEW)),
                    ],
                    [
                        LOCK,
                        f"ALTER TABLE {MESSAGES} DROP CONSTRAINT {ERROR_CK}",
                        error_sql(CODES, ""),
                        f"ALTER TABLE {ACCOUNTS} DROP CONSTRAINT {DETAIL_CK}",
                        detail_sql(DETAILS),
                    ],
                ),
            ],
        ),
    ]
