"""Tareas del Inbox (F6-04b): la salida de los mensajes en cola y el barredor de los que se quedan.

`message.queued` (outbox, F6-04a) trae el id del mensaje; la organización llega en el contexto de
la tarea (`@tenant_task`), nunca en un dato del evento. La entrega es idempotente:
`dispatch_outbound` solo actúa sobre un mensaje `QUEUED`, así que repetir la tarea, o que el
barredor y el evento coincidan, no envía dos veces. Un fallo pasajero del canal reintenta con
espera creciente y acotada; al agotarse queda `FAILED` (`RETRIES_EXHAUSTED`). Ningún registro
lleva el texto.
"""

import datetime as dt
from typing import Any
from uuid import UUID

from django.conf import settings
from django.db import connection
from django.utils import timezone

from apps.conversations import commands, credentials, ingress
from apps.conversations.channels import TemporaryChannelError
from apps.conversations.models import Message
from core.observability.logging import get_logger
from core.outbox import subscribe
from core.outbox.models import OutboxEvent
from core.tenancy.celery import platform_task, tenant_task
from core.tenancy.context import TenantContext, require
from core.tenancy.scope import tenant_scope

logger = get_logger(__name__)
MAX_RETRIES = 5
BACKOFF_SECONDS, BACKOFF_MAX = 30, 600


def deliver(task: Any, message_id: UUID) -> None:
    """Despacha un mensaje; un fallo pasajero reintenta (30 s, 60 s, 120 s… hasta 10 minutos) y, al
    agotar los reintentos, lo deja `FAILED`."""
    try:
        commands.dispatch_outbound(require(), message_id=message_id)
    except TemporaryChannelError as error:
        retries = task.request.retries
        if retries >= MAX_RETRIES:
            logger.warning("inbox.dispatch_gave_up", message_id=str(message_id))
            commands.fail_queued(require(), message_id=message_id, reason="RETRIES_EXHAUSTED")
            return
        countdown = min(BACKOFF_SECONDS * 2**retries, BACKOFF_MAX)
        raise task.retry(exc=error, countdown=countdown, max_retries=MAX_RETRIES) from None


@subscribe("message.queued")
@tenant_task(name="conversations.dispatch_event", bind=True, ignore_result=True, acks_late=True)
def dispatch_event(task: Any, event_id: str) -> None:
    """Handler del outbox: el evento (de esta organización) dice qué mensaje salir."""
    event = OutboxEvent.objects.filter(id=event_id, event_type="message.queued").first()
    if event is not None:
        deliver(task, event.aggregate_id)


@tenant_task(name="conversations.dispatch_message", bind=True, ignore_result=True, acks_late=True)
def dispatch_message(task: Any, message_id: str) -> None:
    """Lo mismo por el id del mensaje: lo usa el barredor."""
    deliver(task, UUID(message_id))


def queued_organizations(before: dt.datetime, limit: int) -> list[UUID]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT public.messages_queued_organizations(%s, %s)", [before, limit])
        return [UUID(str(row[0])) for row in cursor.fetchall()]


@platform_task(name="conversations.sweep_queued", ignore_result=True)
def sweep_queued(batch_size: int = 100, max_orgs: int = 50) -> int:
    """Vuelve a encolar los mensajes `QUEUED` de hace más de `INBOX_SWEEP_AFTER_MINUTES`: los
    eventos anteriores a F6-04b se marcaron publicados sin suscriptores, y un handler puede caer
    tras publicar. Con más de `INBOX_GIVE_UP_AFTER_MINUTES` de antigüedad deja de reintentar y
    el mensaje queda `FAILED` (`RETRIES_EXHAUSTED`): un error inesperado del adaptador no puede
    reintentarse sin fin (sin columna de intentos, la edad es el tope). Descubre las
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
            fresh = found.filter(created_at__gte=give_up).values_list("pk", flat=True)
            ids = list(fresh[:batch_size])
            stale = found.filter(created_at__lt=give_up).values_list("pk", flat=True)
            for message_id in list(stale[:batch_size]):  # sin más reintentos
                commands.fail_queued(tenant, message_id=message_id, reason="RETRIES_EXHAUSTED")
        for message_id in ids:
            dispatch_message.apply_async(
                kwargs={"message_id": str(message_id), "organization_id": str(organization_id)}
            )
        requeued += len(ids)
    return requeued


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
