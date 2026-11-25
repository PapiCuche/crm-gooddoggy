"""Cuentas de WhatsApp: conectar, rotar el token y desconectar (F7-02c; ADR-029 §2, ADR-022). Y
una página de Messenger o una cuenta de Instagram (F7b-04a y F7b-06a, ADR-035 §3), igual.

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

from django.db import IntegrityError, connections, transaction
from django.db.models import QuerySet
from django.db.models.functions import Now
from django.utils import timezone

from apps.access.selectors import AccessDenied, Denied, execution_context, has_permission
from apps.audit.services import Entity, record
from apps.conversations import credentials, expiry, graph
from apps.conversations.models import (
    META_ID,
    PAGE_OWNER,
    SOCIAL,
    TOKEN_FORMAT,
    ChannelAccount,
    Credential,
    Platform,
)
from core.ids import new_id
from core.outbox import emit
from core.tenancy.context import TenantContext
from core.tenancy.scope import require_scope

NAME_MAX = 100
NUMBER_REFUSED = "No se puede registrar ese número."  # el mismo texto, sea cual sea el motivo
ACCOUNT_REFUSED = "No se puede registrar esa cuenta."  # lo mismo para una página o una cuenta
ACCOUNT_FIELDS = ["credential_id", "last_healthcheck_at", "quality_rating", "messaging_limit_tier"]


class BadNumber(ValueError):  # noqa: N818 — un valor, no un fallo
    """El número no se puede registrar. **Un solo error** para un número mal formado y para uno
    que ya existe (en esta organización o en otra): quien conecta no sabe cuál de los dos fue."""


def _allowed(ctx: TenantContext) -> None:
    ectx = execution_context(ctx)  # `AccessDenied` si no es un miembro activo
    if not has_permission(ectx, "channels.manage"):
        raise AccessDenied(Denied.PERMISSION)


def _db_now(alias: str) -> Any:
    """La hora de PostgreSQL (F7b-06j): `last_healthcheck_at` se escribe con ella, la misma con la
    que `channel_accounts_health_due` decide a quién le toca. El reloj del worker no cuenta."""
    with connections[alias].cursor() as cursor:
        cursor.execute("SELECT statement_timestamp()")
        return cursor.fetchone()[0]


def _id(value: Any, what: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(META_ID, value):
        raise BadNumber(NUMBER_REFUSED) if what == "número" else ValueError(f"{what}: no válido")
    return value


def _name(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > NAME_MAX:
        raise ValueError("nombre: de 1 a 100 caracteres de una línea")
    if not value.isprintable():  # una sola línea, sin controles
        raise ValueError("nombre: de 1 a 100 caracteres de una línea")
    return value


def _token(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(TOKEN_FORMAT, value):
        raise ValueError("token: formato no válido")  # sin repetirlo
    return value


REAL = (Platform.WHATSAPP, *SOCIAL)  # las cuentas que Meta puede poner en `ERROR` (ADR-035 §3)


def _live(platform: str = Platform.WHATSAPP) -> QuerySet[ChannelAccount]:
    return ChannelAccount.objects.filter(platform=platform, deleted_at__isnull=True)


def _check(token: str, account: ChannelAccount) -> dict[str, Any]:
    """Que Meta acepte `token` para la misma cuenta: el número en su WABA o, en Messenger e
    Instagram, la misma página (y en Instagram, la misma cuenta vinculada: ADR-035 §3) con un rol
    en ella: conectar la suscribe y rotar lee sus apps suscritas, que también pide el rol."""
    if account.platform == Platform.WHATSAPP:
        return graph.phone_number(
            token, account.external_parent_id or "", account.external_account_id
        )
    instagram = account.platform == Platform.INSTAGRAM
    page = account.external_parent_id if instagram else account.external_account_id
    graph.page(token, page or "", account.external_account_id if instagram else None)
    subscribed = graph.subscription(token, page or "")  # F7b-06e: y un rol en la página
    return {"quality_rating": None, "messaging_limit_tier": None, "subscribed": subscribed}


UNSUBSCRIBED = "PAGE_NOT_SUBSCRIBED"
# F7b-06g (ADR-035 §10): (estado, detalle) de una cuenta social según su página tenga suscrita la
# app del CRM o no. Solo estos pares cambian: `ERROR`, `TOKEN_INVALID`, `MESSAGES_ACCESS_OFF` y lo
# demás siguen igual. Sin suscripción la cuenta aún envía (`DEGRADED`), pero no recibe.
Pair = tuple[str, str | None]
LOST: dict[Pair, Pair] = {
    ("CONNECTED", None): ("DEGRADED", UNSUBSCRIBED),
    ("PENDING", "WEBHOOK_NOT_SEEN"): ("PENDING", UNSUBSCRIBED),
}
BACK: dict[Pair, Pair] = {
    ("DEGRADED", UNSUBSCRIBED): ("CONNECTED", None),
    ("PENDING", UNSUBSCRIBED): ("PENDING", "WEBHOOK_NOT_SEEN"),
}


def _follow(account: ChannelAccount, subscribed: bool | None) -> None:
    """Aplica a la cuenta (ya bloqueada) lo que Meta dice de la suscripción de su página. `None`
    (no se sabe) y WhatsApp no cambian nada."""
    if account.platform in SOCIAL and subscribed is not None:
        key = (account.status, account.status_detail)
        account.status, account.status_detail = (BACK if subscribed else LOST).get(key, key)


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
    record(ctx, action, entity, changes, {"platform": account.platform, **ids})
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
    _name(name)
    _allowed(ctx)
    seen = graph.phone_number(token, waba, number)  # sin tocar la base: antes de guardar nada
    account_id = new_id()
    try:
        with transaction.atomic(using=alias):
            now = _db_now(alias)
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


def connect_social(
    ctx: TenantContext, *, platform: str, name: str, page_id: str, access_token: str,
    instagram_account_id: str | None = None,
) -> tuple[ChannelAccount, Credential]:  # fmt: skip
    """Registra una página de Messenger o la cuenta de Instagram vinculada a una página (ADR-035
    §3), en un paso: valida los formatos, comprueba con Meta que el token gestiona la página (y que
    su cuenta de Instagram es la dada), suscribe la app a la página y guarda la cuenta `PENDING` con
    su credencial cifrada. La cuenta registrada (la página o la de Instagram) mal formada o ya
    registrada en cualquier organización, o su página ya conectada en otra organización con
    cualquiera de las dos plataformas (F7b-06e), es el mismo `BadNumber`; el resto, como
    `connect_whatsapp`.
    """
    alias = require_scope(ctx)
    if platform not in (Platform.MESSENGER, Platform.INSTAGRAM):
        raise ValueError("plataforma: MESSENGER o INSTAGRAM")
    instagram = platform == Platform.INSTAGRAM
    if instagram != (instagram_account_id is not None):
        raise ValueError("la cuenta de Instagram va solo con INSTAGRAM")
    external = instagram_account_id if instagram else page_id
    if not isinstance(external, str) or not re.fullmatch(META_ID, external):
        raise BadNumber(ACCOUNT_REFUSED)
    page, token = _id(page_id, "página"), _token(access_token)
    _name(name)
    _allowed(ctx)
    seen = graph.page(token, page, instagram_account_id)  # antes de guardar nada
    # Antes de la unicidad, que decide la base al guardar: suscribir la misma app a una página ya
    # suscrita no cambia nada en Meta, así que una conexión rechazada después no deja nada nuevo.
    graph.subscribe(token, page)
    known = expiry.read(platform, token)  # F7b-12: `None` si no se sabe, que no impide conectar
    account_id = new_id()
    try:
        with transaction.atomic(using=alias):
            # De una en una por página (la de Facebook en las dos plataformas), ya sin llamadas a
            # Meta: dos organizaciones que la guardan a la vez se esperarían la una a la otra en la
            # `EXCLUDE` (un deadlock, un 500); así, la segunda ve la primera y recibe `BadNumber`.
            # Dos páginas cuyo hash coincide solo se esperan un momento.
            with connections[alias].cursor() as cursor:
                key = f"channel_page:{page}"
                cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", [key])
            credential = credentials.store(ctx, account_id, token, expiry=known)
            account = ChannelAccount.objects.using(alias).create(
                id=account_id, platform=platform, name=name, external_account_id=external,
                external_parent_id=page if instagram else None,
                display_identifier="" if instagram else seen["name"],  # el @usuario: F7b-04c
                status="PENDING", status_detail="WEBHOOK_NOT_SEEN",
                last_healthcheck_at=_db_now(alias), credential_id=credential.pk,
                created_by_user_id=ctx.user_id,
            )  # fmt: skip
            _announce(ctx, "channel_account.connected", account, None, str(credential.pk))
    except IntegrityError as error:  # el error de PostgreSQL (con la RLS, sin valores) no sale
        taken = getattr(getattr(error.__cause__, "diag", None), "constraint_name", "")
        if taken not in ("channel_accounts_external_uq", PAGE_OWNER):
            raise
        raise BadNumber(ACCOUNT_REFUSED) from None
    return account, credential


def _by_quality(account: ChannelAccount, quality: str | None) -> None:
    """La regla de la calidad de WhatsApp (ADR-029 §12), en la comprobación de salud y en la
    rotación (F7b-19): una `PENDING` con el webhook ya visto pasa a `CONNECTED`; `YELLOW` o `RED`
    deja `DEGRADED` (`QUALITY_LOW`) y una calidad recuperada vuelve a `CONNECTED` (solo la degradada
    por calidad). Cambia solo el objeto."""
    low, was = quality in ("YELLOW", "RED"), account.status
    if account.status == "PENDING" and account.status_detail is None:  # el webhook ya se vio
        account.status = "DEGRADED" if low else "CONNECTED"
    elif account.status == "CONNECTED" and low:
        account.status = "DEGRADED"
    elif account.status == "DEGRADED" and account.status_detail == "QUALITY_LOW" and not low:
        account.status = "CONNECTED"
    if account.status != was:
        account.status_detail = "QUALITY_LOW" if account.status == "DEGRADED" else None


def rotate_token(
    ctx: TenantContext, *, channel_account_id: UUID, access_token: str,
    platform: str = Platform.WHATSAPP,
) -> tuple[ChannelAccount, Credential]:  # fmt: skip
    """Cambia el token de una cuenta: el nuevo se comprueba con Meta contra el mismo número y el
    mismo WABA, sustituye al anterior (que se invalida y pierde su cifrado) y limpia
    `TOKEN_INVALID`. Una cuenta `ERROR` por `TOKEN_INVALID` no se comprueba nunca, así que sería un
    callejón sin salida: como Meta acaba de verificar el token nuevo contra su mismo número y WABA
    (lo mismo que la comprobación de salud), sale de `ERROR` como `CONNECTED`. En WhatsApp, después,
    la calidad que Meta acaba de dar se aplica como en la comprobación de salud (`_by_quality`,
    F7b-19): `YELLOW` o `RED` deja `DEGRADED`, y una `PENDING` con el webhook ya visto (sin
    detalle) pasa a `CONNECTED` o `DEGRADED`; la que aún no lo vio (`WEBHOOK_NOT_SEEN`) sigue
    `PENDING`. Ninguna vuelve a `PENDING` desde otro estado (lo prohíbe el disparador
    `channel_accounts_identity_fixed`). En Messenger e Instagram (F7b-06a) se comprueba la misma
    página con `graph.page` y no hay calidad ni nivel.
    `DoesNotExist` si no es una cuenta viva de `platform` de esta organización."""
    alias = require_scope(ctx)
    token = _token(access_token)
    _allowed(ctx)
    account = _live(platform).using(alias).get(pk=channel_account_id)
    seen = _check(token, account)
    social = account.platform in SOCIAL
    ids = {"channel_account_id": str(account.pk)}  # F7b-12c: el registro, con la cuenta
    known = expiry.read(account.platform, token, **ids) if social else None  # F7b-12
    with transaction.atomic(using=alias):
        found = _live(platform).using(alias).select_for_update(no_key=True).annotate(db_now=Now())
        account = found.get(pk=channel_account_id)
        old, status, detail = account.credential_id, account.status, account.status_detail
        credentials.revoke(ctx, old)  # antes de guardar la nueva: una viva por cuenta
        credential = credentials.store(ctx, account.pk, token, rotated=True, expiry=known)
        account.credential_id, account.last_healthcheck_at = credential.pk, account.db_now
        account.quality_rating = seen["quality_rating"]
        account.messaging_limit_tier = seen["messaging_limit_tier"]
        changed: dict[str, Any] = {"credential_id": [str(old) if old else None, str(credential.pk)]}
        if account.status_detail == "TOKEN_INVALID":
            account.status_detail = None
            if account.status == "ERROR":  # un callejón sin salida: nadie la comprueba en `ERROR`
                account.status = "CONNECTED"  # la calidad baja la degrada abajo (`_by_quality`)
        if account.platform in SOCIAL:  # F7b-06g/h: lo que Meta acaba de decir de la página
            account.page_subscribed = seen["subscribed"]
            _follow(account, seen["subscribed"])
        else:  # F7b-19: la calidad que Meta acaba de dar, como la comprobación de salud
            _by_quality(account, seen["quality_rating"])
        if account.status != status:
            changed["status"] = [status, account.status]
        if account.status_detail != detail:  # también uno que se limpia (F7b-06h)
            changed["status_detail"] = [detail, account.status_detail]
        account.version += 1
        fields = [*ACCOUNT_FIELDS, "status", "status_detail", "page_subscribed", "version",
                  "updated_at"]  # fmt: skip
        account.save(using=alias, update_fields=fields)
        _announce(ctx, "channel_account.credentials_rotated", account, changed, str(credential.pk))
    return account, credential


def disconnect(
    ctx: TenantContext, *, channel_account_id: UUID, platform: str = Platform.WHATSAPP
) -> None:
    """Desconecta la cuenta: queda borrada (`DISCONNECTED`, `DISCONNECTED_BY_ADMIN`), su credencial
    se invalida y su número (o su página) se libera. `DoesNotExist` si no es una cuenta viva de
    `platform` de esta organización."""
    alias = require_scope(ctx)
    _allowed(ctx)
    with transaction.atomic(using=alias):
        found = _live(platform).using(alias).select_for_update(no_key=True)
        account = found.get(pk=channel_account_id)
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


def _demote(
    ctx: TenantContext,
    account_id: UUID,
    *,
    only: tuple[str, ...],
    to: str,
    detail: str,
    action: str,
    credential_id: UUID | None = None,
) -> bool:
    """Cambia el estado de una cuenta viva que está en uno de `only` (lo que informa el proveedor,
    sin permiso de persona): `to` con su `detail`, auditado y con evento, solo con ids."""
    alias = require_scope(ctx)
    with transaction.atomic(using=alias):
        real = ChannelAccount.objects.filter(platform__in=REAL, deleted_at__isnull=True)
        found = real.using(alias).select_for_update(no_key=True)
        found = found.filter(pk=account_id, status__in=only)
        if credential_id is not None:  # el resultado es de ESA credencial: ¿se rotó mientras tanto?
            found = found.filter(credential_id=credential_id)
        if (account := found.first()) is None:
            return False
        old, account.status, account.status_detail = account.status, to, detail
        account.version += 1
        account.save(
            using=alias, update_fields=["status", "status_detail", "version", "updated_at"]
        )
        _announce(ctx, action, account, {"status": [old, to]}, None)
        return True


def mark_token_invalid(
    ctx: TenantContext, *, channel_account_id: UUID, credential_id: UUID | None = None
) -> bool:
    """Meta rechazó el token (ADR-029 §8): la cuenta pasa a `ERROR` (`TOKEN_INVALID`) y deja de
    enviar hasta que se rote. `False` si ya estaba `ERROR` o `DISCONNECTED`, o ya no está viva."""
    only = ("PENDING", "CONNECTED", "DEGRADED")
    return _demote(ctx, channel_account_id, only=only, to="ERROR", detail="TOKEN_INVALID",
                   action="channel_account.token_invalid", credential_id=credential_id)  # fmt: skip


def mark_messages_access_off(ctx: TenantContext, *, channel_account_id: UUID) -> bool:
    """El titular de Instagram apagó «Allow access to messages» (ADR-035 §3, `200`/`2534041`): la
    cuenta pasa a `ERROR` (`MESSAGES_ACCESS_OFF`) y deja de enviar hasta que se reconecte. `False`
    si ya estaba `ERROR` o `DISCONNECTED`, o ya no está viva."""
    only = ("PENDING", "CONNECTED", "DEGRADED")
    return _demote(ctx, channel_account_id, only=only, to="ERROR", detail="MESSAGES_ACCESS_OFF",
                   action="channel_account.messages_access_off")  # fmt: skip


def mark_degraded(
    ctx: TenantContext, *, channel_account_id: UUID, credential_id: UUID | None = None
) -> bool:
    """Meta rechazó un envío por la calidad del número (ADR-029 §8): `CONNECTED` pasa a `DEGRADED`
    (`QUALITY_LOW`), con aviso. `False` si no estaba `CONNECTED`; con `credential_id` (F7b-19),
    también si la cuenta ya no usa esa credencial: la rotación posterior al envío aplicó la calidad
    que Meta acababa de dar (`_by_quality`), más reciente que el rechazo."""
    only = ("CONNECTED",)
    return _demote(ctx, channel_account_id, only=only, to="DEGRADED", detail="QUALITY_LOW",
                   action="channel_account.degraded", credential_id=credential_id)  # fmt: skip


CHECKED = ("PENDING", "CONNECTED", "DEGRADED")  # las que se comprueban (ADR-029 §12)


def health_target(
    ctx: TenantContext, *, channel_account_id: UUID
) -> tuple[str, str, str, Credential] | None:
    """Lo que la comprobación de salud necesita de una cuenta viva `PENDING`, `CONNECTED` o
    `DEGRADED`: su plataforma, su WABA (o su página en Messenger e Instagram, F7b-06g), su número
    (o su cuenta) y su credencial viva (la fila cifrada: el token se abre solo durante la llamada a
    Meta). `None` si ya no se comprueba o no tiene credencial."""
    alias = require_scope(ctx)
    real = ChannelAccount.objects.filter(platform__in=REAL, deleted_at__isnull=True)
    account = real.using(alias).filter(pk=channel_account_id, status__in=CHECKED).first()
    live = Credential.objects.using(alias).filter(
        pk=account.credential_id if account else None, owner_id=channel_account_id,
        revoked_at__isnull=True,
    )  # fmt: skip
    if account is None or (credential := live.first()) is None:
        return None
    page = account.external_account_id if account.platform == Platform.MESSENGER else None
    parent = page or account.external_parent_id or ""
    return account.platform, parent, account.external_account_id, credential


def apply_subscription(
    ctx: TenantContext, *, channel_account_id: UUID, credential_id: UUID, subscribed: bool | None
) -> bool:
    """Una comprobación de una cuenta de Messenger o Instagram (F7b-06g, ADR-035 §10): guarda la
    hora y, si la página perdió o recuperó la suscripción de la app, el estado (`_follow`), auditado
    con ids y códigos. Solo si `credential_id` sigue siendo la credencial de la cuenta (no se rotó
    mientras tanto). `False` si ya no se comprueba."""
    alias = require_scope(ctx)
    with transaction.atomic(using=alias):
        real = ChannelAccount.objects.filter(platform__in=SOCIAL, deleted_at__isnull=True)
        found = real.using(alias).select_for_update(no_key=True).annotate(db_now=Now()).filter(
            pk=channel_account_id, status__in=CHECKED, credential_id=credential_id
        )  # fmt: skip
        if (account := found.first()) is None:
            return False
        before = (account.status, account.status_detail)
        _follow(account, subscribed)
        account.last_healthcheck_at, account.page_subscribed = account.db_now, subscribed
        fields = ["last_healthcheck_at", "page_subscribed", "updated_at"]
        now = (account.status, account.status_detail)
        if now != before:
            account.version += 1
            fields += ["status", "status_detail", "version"]
        account.save(using=alias, update_fields=fields)
        if now != before:
            changes = {"status": [before[0], now[0]], "status_detail": [before[1], now[1]]}
            _announce(ctx, "channel_account.health", account, changes, None)
        return True


def note_attempt(ctx: TenantContext, *, channel_account_id: UUID) -> None:
    """Un intento de comprobación que no terminó (F7b-06h): guarda solo su hora, para que la cuenta
    espere su turno como las demás. Ni el estado, ni la última comprobación, ni la versión, ni la
    auditoría: no se supo nada nuevo de la cuenta. La hora es la de la base, la misma con la que
    `channel_accounts_health_due` decide (un intento «futuro» cuenta como ninguno)."""
    alias = require_scope(ctx)
    found = ChannelAccount.objects.using(alias).filter(pk=channel_account_id)
    found.update(health_attempt_at=Now())


def apply_health(
    ctx: TenantContext, *, channel_account_id: UUID, credential_id: UUID, quality: str, tier: str
) -> bool:
    """Una comprobación de salud correcta (ADR-029 §12): guarda la calidad, el nivel de mensajería
    y la hora; una cuenta `PENDING` con el webhook ya visto pasa a `CONNECTED`; calidad `YELLOW` o
    `RED` deja `DEGRADED` (`QUALITY_LOW`) y calidad recuperada vuelve a `CONNECTED` (solo la
    degradada por calidad). Audita y avisa solo si algo cambió, con ids y valores cerrados.
    `False` si la cuenta ya no se comprueba (desconectada, en `ERROR`, borrada) o, como en
    `apply_subscription`, si ya no usa `credential_id` (F7b-20): se rotó durante la llamada a Meta
    y la rotación ya aplicó una lectura más reciente; nada se escribe."""
    alias = require_scope(ctx)
    with transaction.atomic(using=alias):
        found = _live().using(alias).select_for_update(no_key=True).annotate(db_now=Now())
        account = found.filter(
            pk=channel_account_id, status__in=CHECKED, credential_id=credential_id
        ).first()  # fmt: skip
        if account is None:
            return False
        before = {"status": account.status, "quality_rating": account.quality_rating,
                  "messaging_limit_tier": account.messaging_limit_tier}  # fmt: skip
        _by_quality(account, quality)
        account.quality_rating, account.messaging_limit_tier = quality, tier
        changes = {key: [was, getattr(account, key)] for key, was in before.items()
                   if getattr(account, key) != was}  # fmt: skip
        account.last_healthcheck_at = account.db_now  # la hora de la base, leída con el bloqueo
        fields = ["status", "status_detail", "quality_rating", "messaging_limit_tier"]
        if changes:
            account.version += 1
            fields.append("version")
        account.save(using=alias, update_fields=[*fields, "last_healthcheck_at", "updated_at"])
        if changes:
            _announce(ctx, "channel_account.health", account, changes, None)
        return True
