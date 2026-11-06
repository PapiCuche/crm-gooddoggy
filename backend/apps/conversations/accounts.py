"""Cuentas de WhatsApp: conectar, rotar el token y desconectar (F7-02c; ADR-029 §2, ADR-022).

Los únicos escritores de la conexión de una cuenta real; solo la API y las tareas los importan. El
token de acceso lo guarda `apps.conversations.credentials` (cifrado, atado a la cuenta); aquí se
valida contra la Graph API **antes** de guardar nada y no sale: ni a la auditoría, ni al outbox, ni
a un error ni a un registro (solo `last_four` y los ids). Una cuenta real **nace `PENDING`**
(`WEBHOOK_NOT_SEEN`): pasa a `CONNECTED` con el primer evento firmado (F7-03), no por este
comando. Reconectar es crear otra cuenta: la desconectada queda borrada y su número se libera.
Cada comando comprueba `channels.manage` antes de leer nada o de llamar a Meta.
"""

import re
from typing import Any
from uuid import UUID

from django.db import IntegrityError, transaction
from django.db.models import QuerySet
from django.utils import timezone

from apps.access.selectors import AccessDenied, Denied, execution_context, has_permission
from apps.audit.services import Entity, record
from apps.conversations import credentials, graph
from apps.conversations.models import META_ID, TOKEN_FORMAT, ChannelAccount, Credential, Platform
from core.ids import new_id
from core.outbox import emit
from core.tenancy.context import TenantContext
from core.tenancy.scope import require_scope

NAME_MAX = 100
NUMBER_REFUSED = "No se puede registrar ese número."  # el mismo texto, sea cual sea el motivo
ACCOUNT_FIELDS = ["credential_id", "last_healthcheck_at", "quality_rating", "messaging_limit_tier"]


class BadNumber(ValueError):  # noqa: N818 — un valor, no un fallo
    """El número no se puede registrar. **Un solo error** para un número mal formado y para uno
    que ya existe (en esta organización o en otra): quien conecta no sabe cuál de los dos fue."""


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
            credential = credentials.store(ctx, account_id, token)
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
        credentials.revoke(ctx, old)  # antes de guardar la nueva: una viva por cuenta
        credential = credentials.store(ctx, account.pk, token, rotated=True)
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
        credentials.revoke(ctx, account.credential_id)
        account.status, account.status_detail = "DISCONNECTED", "DISCONNECTED_BY_ADMIN"
        account.deleted_at, account.deleted_by_user_id = timezone.now(), ctx.user_id
        account.version += 1
        fields = ["status", "status_detail", "deleted_at", "deleted_by_user_id"]
        account.save(using=alias, update_fields=[*fields, "version", "updated_at"])
        _announce(
            ctx, "channel_account.disconnected", account, {"status": [old, "DISCONNECTED"]}, None
        )
