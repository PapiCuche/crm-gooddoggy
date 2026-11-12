"""Tareas del Inbox (F6-04b, F7-05b): la salida de los mensajes en cola y el barredor de los que
se quedan.

`message.queued` (outbox, F6-04a) trae el id del mensaje. El despachador **reclama** el mensaje
(`QUEUED` → `SENDING`), llama al canal **fuera de toda transacción** y cierra el resultado en otra
(ADR-029 §6): `dispatch_message` es una tarea de plataforma con un `tenant_scope` por paso. Un fallo
pasajero (seguro que no llegó) devuelve el reclamo y reintenta con espera creciente; al agotarse
queda `FAILED` (`RETRIES_EXHAUSTED`). Un resultado ambiguo no se reenvía: sigue `SENDING`. Ningún
registro lleva el texto. `publish_realtime` (F6-07a): tipo e ids al WebSocket del Inbox.
"""

import datetime as dt
import time
from collections import Counter
from collections.abc import Callable
from typing import Any
from uuid import UUID

from django.conf import settings
from django.db import connection
from django.db.models import Q
from django.utils import timezone

from apps.conversations import accounts, commands, credentials, graph, ingress, realtime
from apps.conversations.channels import (
    AmbiguousChannelError,
    ChannelAdapter,
    SendResult,
    TemporaryChannelError,
    UnknownPlatform,
    adapter_for,
)
from apps.conversations.models import Credential, Message
from core import sealed
from core.observability.logging import get_logger
from core.outbox import subscribe
from core.outbox.models import OutboxEvent
from core.tenancy.celery import platform_task, tenant_task
from core.tenancy.context import TenantContext, require
from core.tenancy.scope import tenant_scope

logger = get_logger(__name__)
MAX_RETRIES = 5
BACKOFF_SECONDS, BACKOFF_MAX = 30, 600
REALTIME_RETRIES = 3
HEALTH_PASS_SECONDS = 120  # lo máximo que una pasada de la comprobación de salud retiene un worker
REQUEUE_WAIT = 120  # segundos: lo que el barredor de la cola deja pasar antes de repetir un mensaje


def deliver(
    task: Any, organization_id: UUID, message_id: UUID,
    resolve: Callable[[str], ChannelAdapter] = adapter_for,
) -> None:  # fmt: skip
    """Despacha un mensaje: reclamar, llamar al canal sin transacción y cerrar el resultado."""
    ctx = TenantContext(organization_id, "celery")
    with tenant_scope(ctx):
        claim = commands.claim_outbound(ctx, message_id=message_id)
    if claim is None:  # ya salió, otro lo reclama, o la política lo falló
        return
    try:
        result = resolve(claim.platform).send(claim)
    except UnknownPlatform:
        result = SendResult(False, reason="CHANNEL_UNAVAILABLE")
    except AmbiguousChannelError:  # pudo llegar: no se reenvía; sigue `SENDING`
        return
    except TemporaryChannelError as error:  # seguro que no llegó: se devuelve y se reintenta
        countdown = min(BACKOFF_SECONDS * 2**task.request.retries, BACKOFF_MAX)
        with tenant_scope(ctx):
            wait = dt.timedelta(seconds=countdown)
            attempts = commands.release_outbound(ctx, message_id=message_id, wait=wait)
            if task.request.retries >= MAX_RETRIES or (attempts or 0) > MAX_RETRIES:
                logger.warning("inbox.dispatch_gave_up", message_id=str(message_id))
                commands.fail_queued(ctx, message_id=message_id, reason="RETRIES_EXHAUSTED")
                return
        raise task.retry(exc=error, countdown=countdown, max_retries=MAX_RETRIES) from None
    with tenant_scope(ctx):
        commands.settle_outbound(ctx, message_id=message_id, result=result)
    if result.reason == "TOKEN_INVALID":  # Meta no acepta el token: la cuenta deja de enviar
        with tenant_scope(ctx):  # solo si ese token sigue siendo el de la cuenta (¿se rotó?)
            accounts.mark_token_invalid(
                ctx,
                channel_account_id=claim.account_id,
                credential_id=claim.credential.pk if claim.credential else None,
            )


@subscribe("message.queued")
@tenant_task(name="conversations.dispatch_event", bind=True, ignore_result=True, acks_late=True)
def dispatch_event(task: Any, event_id: str) -> None:
    """Handler del outbox: el evento (de esta organización) dice qué mensaje salir. No despacha
    aquí, dentro de su transacción: encola `dispatch_message`."""
    event = OutboxEvent.objects.filter(id=event_id, event_type="message.queued").first()
    if event is not None:
        ids = {
            "message_id": str(event.aggregate_id),
            "organization_id": str(require().organization_id),
        }
        dispatch_message.apply_async(kwargs=ids)


@subscribe(*realtime.EVENTS)
@tenant_task(name="conversations.publish_realtime", bind=True, ignore_result=True, acks_late=True)
def publish_realtime(task: Any, event_id: str) -> None:
    """Handler del outbox para el WebSocket del Inbox (F6-07a): reenvía solo tipo e ids. Sin Redis
    reintenta y luego descarta (el cliente se pone al día al reconectar)."""
    event = OutboxEvent.objects.filter(id=event_id).first()
    if event is None:
        return
    stamp = event.occurred_at.isoformat()
    message = realtime.outgoing(event.event_type, event.aggregate_id, event.payload, stamp)
    if message is None:
        return
    try:
        sent = realtime.publish(require().organization_id, message)
    except Exception as error:  # noqa: BLE001 — Redis caído: se reintenta; nunca se registra el evento
        if task.request.retries >= REALTIME_RETRIES:
            logger.warning("inbox.realtime_dropped", error=type(error).__name__)
            return
        raise task.retry(countdown=2**task.request.retries, max_retries=REALTIME_RETRIES) from None
    if not sent:
        logger.warning("inbox.realtime_no_layer")


@platform_task(name="conversations.dispatch_message", bind=True, ignore_result=True, acks_late=True)
def dispatch_message(task: Any, message_id: str, organization_id: str) -> None:
    """Despacha un mensaje por su id y su organización (las pone quien encola: el handler o el
    barredor); cada paso abre su `tenant_scope`, así que uno de otra organización no existe."""
    deliver(task, UUID(organization_id), UUID(message_id))


def queued_organizations(before: dt.datetime, limit: int) -> list[UUID]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT public.messages_queued_organizations(%s, %s)", [before, limit])
        return [UUID(str(row[0])) for row in cursor.fetchall()]


@platform_task(name="conversations.sweep_queued", ignore_result=True)
def sweep_queued(batch_size: int = 100, max_orgs: int = 50) -> int:
    """Vuelve a encolar los mensajes `QUEUED` de hace más de `INBOX_SWEEP_AFTER_MINUTES`: los
    eventos anteriores a F6-04b se marcaron publicados sin suscriptores, y un handler puede caer
    tras publicar. No reencola lo que aún espera su `not_before` (un «demasiado rápido» no se
    amplifica) y, con más de `INBOX_GIVE_UP_AFTER_MINUTES` de antigüedad o más de `MAX_RETRIES`
    intentos devueltos, deja de reintentar: queda `FAILED` (`RETRIES_EXHAUSTED`). Descubre las
    organizaciones con una función SECURITY DEFINER (solo ids) y lee los mensajes en el scope de
    cada una."""
    now = timezone.now()
    before = now - dt.timedelta(minutes=settings.INBOX_SWEEP_AFTER_MINUTES)
    give_up = now - dt.timedelta(minutes=settings.INBOX_GIVE_UP_AFTER_MINUTES)
    requeued = 0
    for organization_id in queued_organizations(before, max_orgs):
        tenant = TenantContext(organization_id, "celery")
        with tenant_scope(tenant):
            found = Message.objects.filter(
                direction="OUTBOUND", delivery_status="QUEUED", created_at__lt=before
            ).order_by("id")
            spent = Q(created_at__lt=give_up) | Q(dispatch_attempts__gt=MAX_RETRIES)
            ready = Q(not_before__isnull=True) | Q(not_before__lte=now)
            fresh = found.exclude(spent).filter(ready).values_list("pk", flat=True)
            ids = list(fresh[:batch_size])
            for message_id in list(found.filter(spent).values_list("pk", flat=True)[:batch_size]):
                commands.fail_queued(tenant, message_id=message_id, reason="RETRIES_EXHAUSTED")
            # no se vuelve a reencolar antes de `REQUEUE_WAIT`, y el despacho espera lo mismo
            wait = dt.timedelta(seconds=REQUEUE_WAIT)
            Message.objects.filter(pk__in=ids).update(not_before=now + wait)
        for message_id in ids:
            dispatch_message.apply_async(
                kwargs={"message_id": str(message_id), "organization_id": str(organization_id)},
                countdown=REQUEUE_WAIT + 1,
            )
        requeued += len(ids)
    return requeued


def sending_organizations(before: dt.datetime, limit: int) -> list[UUID]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT public.messages_sending_organizations(%s, %s)", [before, limit])
        return [UUID(str(row[0])) for row in cursor.fetchall()]


@platform_task(name="conversations.sweep_sending", ignore_result=True)
def sweep_sending(batch_size: int = 100, max_orgs: int = 50) -> int:
    """Cierra como `FAILED` (`PROVIDER_UNKNOWN`) los `SENDING` con `sending_at` a
    `INBOX_SENDING_DEADLINE_MINUTES` o más: un resultado ambiguo (tiempo agotado, corte, 5xx) o un
    proceso que cayó tras reclamar. **Nunca reenvía**: pudo haber salido, y la persona decide con el
    riesgo de un duplicado a la vista. Descubre las organizaciones con una función SECURITY DEFINER
    (solo ids) y cierra cada mensaje en el scope de la suya."""
    before = timezone.now() - dt.timedelta(minutes=settings.INBOX_SENDING_DEADLINE_MINUTES)
    closed = 0
    for organization_id in sending_organizations(before, max_orgs):
        tenant = TenantContext(organization_id, "celery")
        with tenant_scope(tenant):
            stuck = Message.objects.filter(
                direction="OUTBOUND", delivery_status="SENDING", sending_at__lte=before
            ).order_by("sending_at", "id")
            for message_id in list(stuck.values_list("pk", flat=True)[:batch_size]):
                done = commands.fail_sending(
                    tenant, message_id=message_id, reason="PROVIDER_UNKNOWN"
                )
                closed += done is not None
    return closed


def health_due(before: dt.datetime, limit: int) -> list[tuple[UUID, UUID]]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT * FROM public.channel_accounts_health_due(%s, %s)", [before, limit])
        return [(UUID(str(org)), UUID(str(account))) for org, account in cursor.fetchall()]


def _reject(ctx: TenantContext, account_id: UUID, credential: Credential) -> str:
    with tenant_scope(ctx):  # solo si la credencial comprobada sigue siendo la de la cuenta
        accounts.mark_token_invalid(ctx, channel_account_id=account_id, credential_id=credential.pk)
    return "REJECTED"


def check_account(organization_id: UUID, account_id: UUID) -> str:
    """Comprueba una cuenta con la Graph API (ADR-029 §12): lee en su scope, llama **sin
    transacción** con el token abierto solo durante la llamada y guarda en otro scope. Meta que
    rechaza el token deja la cuenta en `ERROR`; Meta caída no cambia nada. Devuelve un resultado
    corto (`OK`, `REJECTED`, `UNAVAILABLE`, `SKIPPED`)."""
    ctx = TenantContext(organization_id, "celery")
    with tenant_scope(ctx):
        target = accounts.health_target(ctx, channel_account_id=account_id)
    if target is None:
        return "SKIPPED"
    waba, number, credential = target
    try:
        token = credentials.open_token(credential)
        try:
            seen = graph.phone_number(token, waba, number)
        finally:
            del token
    except sealed.SealError:
        if not sealed.has_key(credential.key_version):
            # La clave de esa versión no está en este despliegue: es de la configuración, no del
            # token. No pone la cuenta en `ERROR` (un despliegue sin la clave vieja las pondría
            # todas); se reintenta cuando la clave vuelva.
            logger.error("channel.healthcheck_key_missing", key_version=credential.key_version)
            return "UNAVAILABLE"
        return _reject(ctx, account_id, credential)
    except graph.GraphRejected:  # el token ya no abre ese número
        return _reject(ctx, account_id, credential)
    except graph.GraphUnavailable, graph.ChannelNotEnabled:
        return "UNAVAILABLE"  # Meta caída: no cambia nada y se reintenta en la siguiente pasada
    with tenant_scope(ctx):
        accounts.apply_health(
            ctx, channel_account_id=account_id, quality=seen["quality_rating"],
            tier=seen["messaging_limit_tier"],
        )  # fmt: skip
    return "OK"


@platform_task(name="conversations.healthcheck_accounts", ignore_result=True)
def healthcheck_accounts(batch_size: int = 50) -> dict[str, int]:
    """Comprueba la salud de las cuentas de WhatsApp a las que les toca (sin comprobación desde
    `CHANNEL_HEALTH_INTERVAL_MINUTES`): descubre (organización, cuenta) con una función SECURITY
    DEFINER (solo ids) y trabaja en el scope de cada una. Un fallo en una cuenta no detiene las
    demás y solo se registra su tipo. Con el canal apagado no hace nada."""
    if not settings.META_ENABLED:
        return {}
    before = timezone.now() - dt.timedelta(minutes=settings.CHANNEL_HEALTH_INTERVAL_MINUTES)
    done: Counter[str] = Counter()
    started = time.monotonic()
    for organization_id, account_id in health_due(before, batch_size):
        if time.monotonic() - started > HEALTH_PASS_SECONDS:
            break  # con Meta lenta, una pasada no ocupa el worker más que esto: sigue la próxima
        try:
            done[check_account(organization_id, account_id)] += 1
        except Exception as error:  # una cuenta no detiene a las demás; nada del error ni del token
            logger.error("channel.healthcheck_failed", error=type(error).__name__)
            done["ERROR"] += 1
    return dict(done)


@platform_task(name="conversations.reseal_credentials")
def reseal_credentials(max_orgs: int = 50, batch_size: int = 100) -> dict[str, int]:
    """Vuelve a cifrar con la clave vigente los tokens de las cuentas de canal que quedaron con
    otra (F7-02b, ADR-022 §5). Devuelve la clave vigente en este proceso y cuántos volvió a
    cifrar, cuántos no pudo abrir y cuántos dejó para otra pasada."""
    return credentials.reseal(max_orgs=max_orgs, batch_size=batch_size)


@platform_task(name="conversations.process_webhook_ingress", ignore_result=True)
def process_webhook_ingress(batch_size: int = 50) -> dict[str, int]:
    """Enruta los ítems recibidos de los webhooks de Meta (F7-03b): reclama lotes, resuelve la
    cuenta y los cierra. Sin tenant al empezar: el de cada ítem sale de la cuenta registrada."""
    return ingress.process(batch_size)


@platform_task(name="conversations.purge_webhook_ingress", ignore_result=True)
def purge_webhook_ingress(days: int = 90, batch_size: int = 1000) -> int:
    """Borra los ítems de los webhooks de más de `days` días: el tope, no el plazo normal."""
    return ingress.purge(days, batch_size)
