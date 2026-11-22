"""La clave del proveedor de IA de una organización (F8-03a; ADR-034 §4, ADR-022): el único
escritor de `ai_provider_accounts` y `ai_provider_keys`. La clave se cifra con `core.sealed`
(propósito `ai.provider_key`, dueño `ai_provider_account:<provider>:<account_id>`: copiada a otra
cuenta u otro proveedor no se abre) y no sale de aquí salvo por `open_key`, en memoria, para la
llamada que la usa. Ningún error, log ni valor devuelto la lleva. No comprueba permisos: los
comandos con `ai.manage`, auditoría y segundo factor llegan con F8-03b y F8-03c.
"""

import re
from uuid import UUID

from django.conf import settings
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from apps.ai_gateway.models import (
    KEY_PURPOSE,
    AccountStatus,
    AIProviderAccount,
    AIProviderKey,
    Provider,
)
from core import sealed
from core.observability import reporting
from core.observability.logging import get_logger
from core.tenancy.context import TenantContext
from core.tenancy.scope import require_scope, tenant_scope

logger = get_logger(__name__)
KEY_FORMAT = re.compile(r"[A-Za-z0-9_-]{20,256}")  # la forma de las claves de API conocidas


class MalformedKey(ValueError):  # noqa: N818 — nunca lleva la clave
    """La clave no tiene la forma de una clave de API."""


class AccountConflict(Exception):  # noqa: N818 — una carrera, no un fallo
    """Otra operación creó a la vez la cuenta de ese proveedor: se puede reintentar."""


class UnreadableKeys(Exception):  # noqa: N818 — solo lleva cuántas
    """Hay claves guardadas que no se pueden abrir."""


def owner(provider: str, account_id: UUID) -> str:
    return f"ai_provider_account:{provider}:{account_id}"


def _seal(provider: str, account_id: UUID, plain: bytes) -> tuple[bytes, int]:
    box = sealed.seal(KEY_PURPOSE, owner(provider, account_id), plain)
    return box, int.from_bytes(box[1:3], "big")


def _wipe(alias: str, account_id: UUID) -> int:
    """Invalida la clave viva de la cuenta: el cifrado se borra. Devuelve cuántas invalidó."""
    live = AIProviderKey.objects.using(alias).filter(account_id=account_id, revoked_at__isnull=True)
    return live.update(ciphertext=None, revoked_at=timezone.now())


def _locked(alias: str, provider: str) -> AIProviderAccount | None:
    """La cuenta de `provider`, bloqueada hasta el final de la transacción, o `None`."""
    found: AIProviderAccount | None = (
        AIProviderAccount.objects.using(alias).select_for_update().filter(provider=provider).first()
    )
    return found


def store(ctx: TenantContext, provider: str, key: str) -> AIProviderAccount:
    """Cifra y guarda la clave de la cuenta de `provider` de la organización del contexto, que se
    crea si no existe. Si ya tenía una viva, la invalida (rotación); si estaba desactivada, la
    reactiva. Una cuenta nueva nace con el modelo de `AI_PROVIDER_PARAMS`; rotar o reactivar no
    cambia el suyo. Dos llamadas con la misma clave dan cifrados distintos. `AccountConflict` si
    otra operación crea la misma cuenta a la vez."""
    if provider not in Provider.values:
        raise ValueError("proveedor desconocido")
    if not KEY_FORMAT.fullmatch(key):
        raise MalformedKey
    alias = require_scope(ctx)
    now = timezone.now()
    with transaction.atomic(using=alias):
        accounts = AIProviderAccount.objects.using(alias)
        account = _locked(alias, provider)
        if account is None:
            try:
                with transaction.atomic(using=alias):  # sin la carrera, la transacción sigue
                    account = accounts.create(
                        provider=provider, key_last_four=key[-4:],
                        model=settings.AI_PROVIDER_PARAMS[provider]["model"],
                        configured_by_user_id=ctx.user_id, configured_at=now,
                    )  # fmt: skip
            except IntegrityError as error:
                constraint = getattr(getattr(error.__cause__, "diag", None), "constraint_name", "")
                if constraint != "ai_provider_accounts_provider_uq":
                    raise
                raise AccountConflict from None
        else:
            rotated = _wipe(alias, account.pk) > 0
            accounts.filter(pk=account.pk).update(
                status=AccountStatus.ACTIVE, revoked_at=None, key_last_four=key[-4:],
                configured_by_user_id=ctx.user_id, configured_at=now,
                rotated_at=now if rotated else account.rotated_at, updated_at=now,
            )  # fmt: skip
        box, version = _seal(account.provider, account.pk, key.encode())
        AIProviderKey.objects.using(alias).create(
            account_id=account.pk, provider=account.provider, ciphertext=box, key_version=version
        )
        stored: AIProviderAccount = accounts.get(pk=account.pk)
    return stored


def revoke(ctx: TenantContext, provider: str) -> bool:
    """Desactiva la cuenta de `provider` e invalida su clave (el cifrado se borra). Devuelve si
    había una cuenta activa."""
    alias = require_scope(ctx)
    with transaction.atomic(using=alias):
        accounts = AIProviderAccount.objects.using(alias)
        account = _locked(alias, provider)
        if account is None or account.status != AccountStatus.ACTIVE:
            return False
        _wipe(alias, account.pk)
        now = timezone.now()
        accounts.filter(pk=account.pk).update(
            status=AccountStatus.DISABLED, revoked_at=now, updated_at=now
        )
    return True


def open_key(ctx: TenantContext, provider: str) -> str:
    """La clave en claro de la cuenta activa de `provider`, **solo** para la llamada que la usa:
    no se guarda ni se registra. `sealed.SealError` si la cuenta no está activa, no hay una clave
    viva o no se abre."""
    alias = require_scope(ctx)
    active = AIProviderAccount.objects.using(alias).filter(
        provider=provider, status=AccountStatus.ACTIVE
    )
    row = (
        AIProviderKey.objects.using(alias)
        .filter(provider=provider, revoked_at__isnull=True, ciphertext__isnull=False)
        .filter(account_id__in=active.values("pk"))
        .first()
    )
    if row is None or row.ciphertext is None:
        raise sealed.SealError
    return sealed.unseal(KEY_PURPOSE, owner(row.provider, row.account_id), row.ciphertext).decode()


def reseal(*, max_orgs: int = 50, batch_size: int = 100) -> dict[str, int]:
    """Vuelve a cifrar con la clave maestra vigente las claves vivas cifradas con otra (ADR-022
    §5, paso 2), como `apps.conversations.credentials.reseal`: descubre las organizaciones con una
    función SECURITY DEFINER (solo ids) y trabaja en el scope de cada una, fila a fila y
    bloqueada; solo cambian `ciphertext` y `key_version`. Lo que no abre se cuenta
    (`unreadable`, con un reporte) y lo que otra operación tiene bloqueado, también (`busy`). Sin
    claves maestras lanza `ImproperlyConfigured`. Una organización con claves que no se abren
    sigue saliendo en el descubrimiento: con el tope `max_orgs` puede dejar fuera a otras hasta
    que se arregle la clave maestra."""
    done = {"key": sealed.current(), "resealed": 0, "unreadable": 0, "busy": 0}
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT public.ai_provider_keys_stale_organizations(%s, %s)", [done["key"], max_orgs]
        )
        organizations = [UUID(str(row[0])) for row in cursor.fetchall()]
    for organization_id in organizations:
        with tenant_scope(TenantContext(organization_id, "celery")) as ctx:
            alias = require_scope(ctx)
            live = AIProviderKey.objects.using(alias).filter(revoked_at__isnull=True)
            stale = live.exclude(key_version=done["key"]).values_list("pk", flat=True)
            for pk in list(stale[:batch_size]):
                with transaction.atomic(using=alias):
                    row = live.select_for_update(skip_locked=True).filter(pk=pk).first()
                    if row is None:
                        done["busy"] += int(live.filter(pk=pk).exists())
                        continue
                    if not sealed.stale(row.ciphertext or b""):
                        continue
                    bound = owner(row.provider, row.account_id)
                    try:
                        plain = sealed.unseal(KEY_PURPOSE, bound, row.ciphertext or b"")
                    except sealed.SealError:
                        done["unreadable"] += 1
                        continue
                    box, version = _seal(row.provider, row.account_id, plain)
                    live.filter(pk=pk).update(ciphertext=box, key_version=version)
                    done["resealed"] += 1
    logger.info("ai.provider_keys_resealed", **done)
    if done["unreadable"]:
        try:
            error = UnreadableKeys(done["unreadable"])
            reporting.reporter().capture_exception(error, {"action": "ai.provider_keys_reseal"})
        except Exception:
            logger.exception("ai.provider_keys_unreadable_not_reported")
    return done
