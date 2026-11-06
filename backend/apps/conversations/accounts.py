"""Cuentas de WhatsApp: conectar, rotar el token y desconectar (F7-02b; ADR-029 §2, ADR-022).

Los únicos escritores de `credentials` y de la conexión de una cuenta real; solo la API y las tareas
los importan. El token de acceso se cifra con `core.sealed` (propósito `channel.access_token`,
dueño `channel_account:<id>`: copiado a otra cuenta no se abre), se valida contra la Graph API
**antes** de guardar nada, y no sale de aquí: ni a la auditoría, ni al outbox, ni a un error ni a
un registro (solo `last_four`, la huella y los ids). Una cuenta real **nace `PENDING`**
(`WEBHOOK_NOT_SEEN`): pasa a `CONNECTED` con el primer evento firmado (F7-03), no por este
comando. Reconectar es crear otra cuenta: la desconectada queda borrada y su número se libera.
Cada comando comprueba `channels.manage` antes de leer nada o de llamar a Meta.
"""

import hashlib
import re
from typing import Any
from uuid import UUID

from django.db import IntegrityError, connection, transaction
from django.db.models import QuerySet
from django.utils import timezone

from apps.access.selectors import AccessDenied, Denied, execution_context, has_permission
from apps.audit.services import Entity, record
from apps.conversations import graph
from apps.conversations.models import (
    META_ID,
    TOKEN_FORMAT,
    TOKEN_PURPOSE,
    ChannelAccount,
    Credential,
    Platform,
)
from core import sealed
from core.ids import new_id
from core.observability import reporting
from core.observability.logging import get_logger
from core.outbox import emit
from core.tenancy.context import TenantContext
from core.tenancy.scope import require_scope, tenant_scope

logger = get_logger(__name__)
NAME_MAX = 100
NUMBER_REFUSED = "No se puede registrar ese número."  # el mismo texto, sea cual sea el motivo
ACCOUNT_FIELDS = ["credential_id", "last_healthcheck_at", "quality_rating", "messaging_limit_tier"]


class BadNumber(ValueError):  # noqa: N818 — un valor, no un fallo
    """El número no se puede registrar. **Un solo error** para un número mal formado y para uno
    que ya existe (en esta organización o en otra): quien conecta no sabe cuál de los dos fue."""


class UnreadableCredentials(Exception):  # noqa: N818 — solo lleva cuántas
    """Hay credenciales guardadas que no se pueden abrir."""


def owner(account_id: UUID) -> str:
    return f"channel_account:{account_id}"


def _allowed(ctx: TenantContext) -> None:
    ectx = execution_context(ctx)  # `AccessDenied` si no es un miembro activo
    if not has_permission(ectx, "channels.manage"):
        raise AccessDenied(Denied.PERMISSION)


def _id(value: Any, what: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(META_ID, value):
        raise BadNumber(NUMBER_REFUSED) if what == "número" else ValueError(f"{what}: no válido")
    return value


def _token(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(TOKEN_FORMAT, value):
        raise ValueError("token: formato no válido")  # sin repetirlo
    return value


def _live() -> QuerySet[ChannelAccount]:
    return ChannelAccount.objects.filter(platform=Platform.WHATSAPP, deleted_at__isnull=True)


def _store(
    ctx: TenantContext, alias: str, account_id: UUID, token: str, rotated: bool
) -> Credential:
    """Cifra y guarda el token. Dos llamadas con el mismo token dan cifrados distintos."""
    box = sealed.seal(TOKEN_PURPOSE, owner(account_id), token.encode())
    made: Credential = Credential.objects.using(alias).create(
        owner_id=account_id, ciphertext=box, key_version=int.from_bytes(box[1:3], "big"),
        last_four=token[-4:], fingerprint=hashlib.sha256(token.encode()).hexdigest(),
        rotated_at=timezone.now() if rotated else None, created_by_user_id=ctx.user_id,
    )  # fmt: skip
    return made


def _announce(
    ctx: TenantContext,
    action: str,
    account: ChannelAccount,
    changes: dict[str, Any] | None,
    credential_id: str | None,
) -> None:
    """Auditoría y outbox del hecho: solo ids y estados, nunca el token ni su cifrado."""
    ids = {"credential_id": credential_id} if credential_id else {}
    entity = Entity("channel_account", account.pk, account.name)
    record(ctx, action, entity, changes, {"platform": "WHATSAPP", **ids})
    emit(ctx, action, aggregate_type="channel_account", aggregate_id=account.pk, payload=ids)


def _revoke(alias: str, credential_id: UUID | None) -> None:
    """Invalida la credencial: el cifrado se borra, no queda un token antiguo en la base."""
    if credential_id is not None:
        Credential.objects.using(alias).filter(pk=credential_id).update(
            ciphertext=None, revoked_at=timezone.now()
        )


def connect_whatsapp(
    ctx: TenantContext, *, name: str, phone_number_id: str, waba_id: str, access_token: str
) -> tuple[ChannelAccount, Credential]:
    """Registra un número de WhatsApp: valida el formato, comprueba con Meta que el token abre ese
    número en ese WABA y guarda la cuenta `PENDING` con su credencial cifrada. `BadNumber` (mal
    formado, o ya registrado en cualquier organización: el mismo error), `ValueError`,
    `graph.GraphRejected`, `graph.GraphUnavailable`, `graph.ChannelNotEnabled`, `AccessDenied`."""
    alias = require_scope(ctx)
    number, waba, token = _id(phone_number_id, "número"), _id(waba_id, "WABA"), _token(access_token)
    if (
        not isinstance(name, str)
        or not name.strip()
        or len(name) > NAME_MAX
        or not name.isprintable()
    ):
        raise ValueError("nombre: de 1 a 100 caracteres de una línea")
    _allowed(ctx)
    seen = graph.phone_number(token, waba, number)  # sin tocar la base: antes de guardar nada
    account_id, now = new_id(), timezone.now()
    try:
        with transaction.atomic(using=alias):
            credential = _store(ctx, alias, account_id, token, rotated=False)
            account = ChannelAccount.objects.using(alias).create(
                id=account_id, platform=Platform.WHATSAPP, name=name, external_account_id=number,
                external_parent_id=waba, display_identifier=seen["display"], status="PENDING",
                status_detail="WEBHOOK_NOT_SEEN", quality_rating=seen["quality_rating"],
                messaging_limit_tier=seen["messaging_limit_tier"], last_healthcheck_at=now,
                credential_id=credential.pk, created_by_user_id=ctx.user_id,
            )  # fmt: skip
            _announce(ctx, "channel_account.connected", account, None, str(credential.pk))
    except IntegrityError as error:
        if getattr(getattr(error.__cause__, "diag", None), "constraint_name", "") != (
            "channel_accounts_external_uq"
        ):
            raise
        raise BadNumber(NUMBER_REFUSED) from None  # sin el nombre del índice ni el valor
    return account, credential


def rotate_token(
    ctx: TenantContext, *, channel_account_id: UUID, access_token: str
) -> tuple[ChannelAccount, Credential]:
    """Cambia el token de una cuenta: el nuevo se comprueba con Meta contra el mismo número y el
    mismo WABA, sustituye al anterior (que se invalida y pierde su cifrado) y limpia
    `TOKEN_INVALID`. No cambia el estado: volver a `CONNECTED` es de la comprobación de salud.
    `DoesNotExist` si no es una cuenta viva de WhatsApp de esta organización."""
    alias = require_scope(ctx)
    token = _token(access_token)
    _allowed(ctx)
    account = _live().using(alias).get(pk=channel_account_id)
    seen = graph.phone_number(token, account.external_parent_id or "", account.external_account_id)
    with transaction.atomic(using=alias):
        account = _live().using(alias).select_for_update(no_key=True).get(pk=channel_account_id)
        old = account.credential_id
        _revoke(alias, old)  # antes de guardar la nueva: una viva por cuenta
        credential = _store(ctx, alias, account.pk, token, rotated=True)
        account.credential_id, account.last_healthcheck_at = credential.pk, timezone.now()
        account.quality_rating = seen["quality_rating"]
        account.messaging_limit_tier = seen["messaging_limit_tier"]
        if account.status_detail == "TOKEN_INVALID":
            account.status_detail = None
        account.version += 1
        account.save(
            using=alias, update_fields=[*ACCOUNT_FIELDS, "status_detail", "version", "updated_at"]
        )
        changed = {"credential_id": [str(old) if old else None, str(credential.pk)]}
        _announce(ctx, "channel_account.credentials_rotated", account, changed, str(credential.pk))
    return account, credential


def disconnect(ctx: TenantContext, *, channel_account_id: UUID) -> None:
    """Desconecta la cuenta: queda borrada (`DISCONNECTED`, `DISCONNECTED_BY_ADMIN`), su credencial
    se invalida y su número se libera. `DoesNotExist` si no es una cuenta viva de WhatsApp de esta
    organización."""
    alias = require_scope(ctx)
    _allowed(ctx)
    with transaction.atomic(using=alias):
        account = _live().using(alias).select_for_update(no_key=True).get(pk=channel_account_id)
        old = account.status
        _revoke(alias, account.credential_id)
        account.status, account.status_detail = "DISCONNECTED", "DISCONNECTED_BY_ADMIN"
        account.deleted_at, account.deleted_by_user_id = timezone.now(), ctx.user_id
        account.version += 1
        fields = ["status", "status_detail", "deleted_at", "deleted_by_user_id"]
        account.save(using=alias, update_fields=[*fields, "version", "updated_at"])
        _announce(
            ctx, "channel_account.disconnected", account, {"status": [old, "DISCONNECTED"]}, None
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
