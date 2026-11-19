"""El correo de una invitación (F2-130d, ADR-020 §3, ADR-019 §4): lo que hace la tarea
`members.send_invitation`, la única pieza que ve el enlace. Lo genera, guarda su SHA-256, confirma
y solo entonces lo pone en el correo. No lo registra, no lo audita y no lo devuelve.

En el `tenant_scope` de la organización del evento, con actor `SYSTEM`: el evento releído (no
cambia) y, en el orden de invitar, revocar y aceptar, el bloqueo de RBAC y la fila bloqueada. Una
invitación que ya no se podría aceptar, o que llegó a un tope, no se envía. Si la entrega falla,
el enlace ya cuenta y vale, y no se reintenta: otro secreto sería otro envío, y la tarea no espera
15 minutos; quien invita lo vuelve a pedir.
"""

import math
from datetime import datetime
from uuid import UUID

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.utils import timezone

from apps.access.selectors import AccessDenied
from apps.access.services import UnknownRole, ensure_inviter_can_grant, lock_rbac
from apps.accounts.recovery import token_hash
from apps.members.invitation_links import new_secret
from apps.organizations import services as organizations
from core import mail
from core.observability.logging import get_logger
from core.outbox.models import OutboxEvent
from core.tenancy.context import ActorType, TenantContext
from core.tenancy.scope import tenant_scope

logger = get_logger(__name__)
LINK_PATH = "/invitacion"  # la pantalla que lee el secreto del fragmento (F2-130c)
SUBJECT = "Te invitaron a Good Doggy CRM"  # fijo: nada que escriba una organización
BODY = """Hola:

Te invitaron a unirte a «{organization}» en Good Doggy CRM. Para aceptar la invitación, abre
este enlace:

{link}

El enlace vale {days} y una sola vez. Si te envían otro, este deja de valer.

Si no esperabas esta invitación, no hagas nada: no se crea ninguna cuenta hasta que la aceptes.
"""
Issued = tuple[str, str, str, datetime]  # secreto, organización, correo y caducidad


def _issue(ctx: TenantContext, invitation_id: UUID, event_id: UUID) -> Issued | str:
    """Dentro del scope: comprueba, genera el secreto y guarda su hash. Devuelve lo que lleva el
    correo, o la razón por la que no se envía."""
    events = OutboxEvent.objects.filter(
        pk=event_id, event_type=organizations.SEND_REQUESTED, aggregate_id=invitation_id
    )
    requested_at = events.values_list("occurred_at", flat=True).first()  # no cambia: sin bloqueo
    if requested_at is None:
        return "event"
    try:
        lock_rbac(ctx)
        found = organizations.invitation_to_send(
            ctx, invitation_id=invitation_id, requested_at=requested_at
        )
        if isinstance(found, str):
            return found
        name, invitation = found
        ensure_inviter_can_grant(ctx, invitation.invited_by_id, invitation.role_ids)
    except AccessDenied, UnknownRole:  # el enlace no se podría aceptar (ADR-020 §2)
        return "inviter"
    if not mail.deliverable(invitation.email):
        return "address"
    secret = new_secret()
    organizations.store_invitation_link(
        ctx, invitation_id=invitation_id, token_hash=token_hash(secret)
    )
    return secret, name, invitation.email, invitation.expires_at


def send(organization_id: UUID, invitation_id: UUID, event_id: UUID) -> str:
    """Emite el enlace de la invitación que pidió el evento `event_id` y lo envía. Devuelve
    `sent` o la razón por la que no envió nada (ver `_issue`). Una entrega repetida del mismo
    evento no envía otra vez (`served`).

    Sin `APP_ORIGIN` lanza `ImproperlyConfigured` antes de mirar nada. Si la entrega falla lanza
    `MailError`, con el enlace ya guardado y contado: vale aunque no haya llegado.
    """
    if not settings.APP_ORIGIN:
        raise ImproperlyConfigured("APP_ORIGIN es obligatorio para emitir un enlace")
    ctx = TenantContext(organization_id, "celery", actor_type=ActorType.SYSTEM)
    with tenant_scope(ctx):
        issued = _issue(ctx, invitation_id, event_id)
    ids = {"invitation_id": str(invitation_id), "organization_id": str(organization_id)}
    if isinstance(issued, str):
        logger.info("invitation.send_skipped", reason=issued, **ids)
        return issued
    secret, name, email, expires_at = issued  # confirmado: ahora sí, el servidor de correo
    days = max(1, math.ceil((expires_at - timezone.now()).total_seconds() / 86_400))
    # El secreto va en el fragmento: el navegador no lo envía al servidor (ADR-020 §3).
    link = f"{settings.APP_ORIGIN}{LINK_PATH}#{secret}"
    body = BODY.format(organization=name, link=link, days=f"{days} día{'s' * (days != 1)}")
    mail.send(mail.Message(to=email, subject=SUBJECT, body=body, purpose="invitation"))
    logger.info("invitation.sent", **ids)
    return "sent"
