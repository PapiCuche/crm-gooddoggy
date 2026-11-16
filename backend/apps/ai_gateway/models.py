"""Cuentas de proveedor de IA y su clave sellada (F8-03a; ADR-034 §4, ADR-022). Tablas de tenant
con FORCE RLS y FKs compuestas en la migración. La clave solo existe cifrada en `ai_provider_keys`;
lo único a la vista es `key_last_four`. Nada se borra: una clave se invalida (cifrado borrado) y la
cuenta se desactiva. El modelo elegido llega con la lista permitida (F8-05)."""

from django.db import models
from django.db.models.functions import Length
from django.db.models.lookups import Exact, GreaterThanOrEqual, LessThanOrEqual

from core.db.models import TenantModel, uuid7_primary_key

# Lo que mide un valor de `core.sealed` (ADR-022 §3): de 32 a 4127 bytes y el formato 1 al frente.
SEALED_MIN, SEALED_MAX, SEALED_FORMAT = 32, 4127, b"\x01"
KEY_PURPOSE = "ai.provider_key"  # el propósito de `core.sealed`, no un secreto


class Provider(models.TextChoices):
    ANTHROPIC = "ANTHROPIC"  # OpenAI llega con F8-10 (ADR-034 §3)


class AccountStatus(models.TextChoices):
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"


class AIProviderAccount(TenantModel):
    """La cuenta de la organización en un proveedor: una por proveedor. `provider` no cambia
    (UPDATE por columna): forma parte del dueño del cifrado de su clave."""

    id = uuid7_primary_key()
    provider = models.CharField(max_length=16, choices=Provider.choices)
    status = models.CharField(max_length=16, choices=AccountStatus.choices, default="ACTIVE")
    key_last_four = models.CharField(max_length=4)
    configured_by_user_id = models.UUIDField(null=True, blank=True)  # FK a membresías (0001)
    configured_at = models.DateTimeField()  # quién guardó la clave vigente, y cuándo
    rotated_at = models.DateTimeField(null=True, blank=True)  # cuándo sustituyó a otra viva
    revoked_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "ai_provider_accounts"
        constraints = [
            models.UniqueConstraint(
                fields=["organization_id", "provider"], name="ai_provider_accounts_provider_uq"
            ),
            models.UniqueConstraint(  # destino de la FK compuesta de la clave
                fields=["organization_id", "id", "provider"], name="ai_provider_accounts_key_uq"
            ),
            models.CheckConstraint(
                condition=models.Q(provider__in=Provider.values),
                name="ai_provider_accounts_provider_ck",
            ),
            models.CheckConstraint(  # activa sin revocar; desactivada con la fecha
                condition=models.Q(status="ACTIVE", revoked_at__isnull=True)
                | models.Q(status="DISABLED", revoked_at__isnull=False),
                name="ai_provider_accounts_status_ck",
            ),
            models.CheckConstraint(
                condition=models.Q(key_last_four__regex=r"^.{4}$"),
                name="ai_provider_accounts_last_four_ck",
            ),
        ]

    def __str__(self) -> str:
        return str(self.pk)


class AIProviderKey(TenantModel):
    """La clave de una cuenta, cifrada con `core.sealed` (propósito `ai.provider_key`, dueño
    `ai_provider_account:<provider>:<account_id>`). Una viva por cuenta. Al rotar o desactivar se
    **invalida**: `revoked_at` y el cifrado, borrado. Solo cambian `ciphertext`, `key_version` y
    `revoked_at` (UPDATE por columna)."""

    id = uuid7_primary_key()
    account_id = models.UUIDField()  # FK compuesta con el proveedor (0001)
    provider = models.CharField(max_length=16, choices=Provider.choices)
    ciphertext = models.BinaryField(null=True)
    key_version = models.PositiveSmallIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "ai_provider_keys"
        constraints = [
            models.UniqueConstraint(  # una viva por cuenta
                fields=["organization_id", "account_id"],
                condition=models.Q(revoked_at__isnull=True),
                name="ai_provider_keys_live_uq",
            ),
            models.CheckConstraint(  # viva: cifrada con el formato vigente; invalidada: sin cifrado
                condition=models.Q(
                    models.Q(revoked_at__isnull=True, ciphertext__isnull=False)
                    & models.Q(GreaterThanOrEqual(Length("ciphertext"), SEALED_MIN))
                    & models.Q(LessThanOrEqual(Length("ciphertext"), SEALED_MAX))
                    & models.Q(
                        Exact(
                            models.Func(
                                "ciphertext",
                                1,
                                1,
                                function="substring",
                                output_field=models.BinaryField(),
                            ),
                            models.Value(SEALED_FORMAT, output_field=models.BinaryField()),
                        )
                    )  # fmt: skip
                    | models.Q(revoked_at__isnull=False, ciphertext__isnull=True)
                ),
                name="ai_provider_keys_ciphertext_ck",
            ),
            models.CheckConstraint(
                condition=models.Q(key_version__gte=1), name="ai_provider_keys_version_ck"
            ),
        ]

    def __str__(self) -> str:
        return str(self.pk)
