"""Las credenciales de las cuentas de canal (F7-02b; ADR-022, ADR-029 §2): el único escritor de
`credentials`. El token de acceso se cifra con `core.sealed` (propósito `channel.access_token`,
dueño `channel_account:<id>`: copiado a otra cuenta no se abre) y no sale de aquí: lo único que se
guarda a la vista es `last_four`. Los comandos de la cuenta (conectar,
rotar, desconectar) llegan con F7-02c y usan estas funciones. Solo la API y las tareas lo importan.
"""

from uuid import UUID

from django.db import connection, transaction
from django.utils import timezone

from apps.conversations.models import TOKEN_PURPOSE, Credential
from core import sealed
from core.observability import reporting
from core.observability.logging import get_logger
from core.tenancy.context import TenantContext
from core.tenancy.scope import require_scope, tenant_scope

logger = get_logger(__name__)


class UnreadableCredentials(Exception):  # noqa: N818 — solo lleva cuántas
    """Hay credenciales guardadas que no se pueden abrir."""


def owner(account_id: UUID) -> str:
    return f"channel_account:{account_id}"


def store(ctx: TenantContext, account_id: UUID, token: str, *, rotated: bool = False) -> Credential:
    """Cifra y guarda el token de la cuenta `account_id`. Dos llamadas con el mismo token dan
    cifrados distintos. No comprueba el token ni el permiso: es de quien conecta la cuenta."""
    alias = require_scope(ctx)
    box = sealed.seal(TOKEN_PURPOSE, owner(account_id), token.encode())
    made: Credential = Credential.objects.using(alias).create(
        owner_id=account_id, ciphertext=box, key_version=int.from_bytes(box[1:3], "big"),
        last_four=token[-4:],
        rotated_at=timezone.now() if rotated else None, created_by_user_id=ctx.user_id,
    )  # fmt: skip
    return made


def revoke(ctx: TenantContext, credential_id: UUID | None) -> None:
    """Invalida la credencial: el cifrado se borra, no queda un token antiguo en la base."""
    if credential_id is not None:
        Credential.objects.using(require_scope(ctx)).filter(pk=credential_id).update(
            ciphertext=None, revoked_at=timezone.now()
        )


def open_token(credential: Credential) -> str:
    """El token en claro, **solo** para la llamada que lo usa (el adaptador de salida, F7-05): no
    se guarda ni se registra. `sealed.SealError` si la credencial está invalidada o no se abre."""
    if credential.ciphertext is None:
        raise sealed.SealError
    plain = sealed.unseal(TOKEN_PURPOSE, owner(credential.owner_id), credential.ciphertext)
    return plain.decode()


def reseal(*, max_orgs: int = 50, batch_size: int = 100) -> dict[str, int]:
    """Vuelve a cifrar con la clave vigente las credenciales vivas cifradas con otra (ADR-022 §5,
    paso 2). Descubre las organizaciones con una función SECURITY DEFINER (solo ids) y trabaja en
    el scope de cada una, fila a fila y bloqueada; solo cambian `ciphertext` y `key_version`. Lo
    que no abre se deja como está y se cuenta (`unreadable`); lo que otra operación tiene
    bloqueado, también (`busy`). Devuelve la clave vigente en este proceso (`key`) y los
    contadores; sin claves lanza `ImproperlyConfigured`. Una organización con credenciales que no
    se abren sigue saliendo en el descubrimiento: el tope `max_orgs` puede dejar fuera a otras
    hasta que se arregle la clave."""
    done = {"key": sealed.current(), "resealed": 0, "unreadable": 0, "busy": 0}
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT public.credentials_stale_organizations(%s, %s)", [done["key"], max_orgs]
        )
        organizations = [UUID(str(row[0])) for row in cursor.fetchall()]
    for organization_id in organizations:
        with tenant_scope(TenantContext(organization_id, "celery")) as ctx:
            alias = require_scope(ctx)
            live = Credential.objects.using(alias).filter(revoked_at__isnull=True)
            for pk in list(
                live.exclude(key_version=done["key"]).values_list("pk", flat=True)[:batch_size]
            ):
                with transaction.atomic(using=alias):
                    row = live.select_for_update(skip_locked=True).filter(pk=pk).first()
                    if row is None:
                        done["busy"] += int(live.filter(pk=pk).exists())
                        continue
                    if not sealed.stale(row.ciphertext or b""):
                        continue
                    try:
                        token = sealed.unseal(
                            TOKEN_PURPOSE, owner(row.owner_id), row.ciphertext or b""
                        )
                    except sealed.SealError:
                        done["unreadable"] += 1
                        continue
                    box = sealed.seal(TOKEN_PURPOSE, owner(row.owner_id), token)
                    live.filter(pk=pk).update(
                        ciphertext=box, key_version=int.from_bytes(box[1:3], "big")
                    )
                    done["resealed"] += 1
    logger.info("channel.credentials_resealed", **done)
    if done["unreadable"]:
        try:
            error = UnreadableCredentials(done["unreadable"])
            reporting.reporter().capture_exception(error, {"action": "channel.credentials_reseal"})
        except Exception:
            logger.exception("channel.credentials_unreadable_not_reported")
    return done
