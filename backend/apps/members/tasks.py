"""Tareas de `members` (autodiscover de Celery): el correo de una invitación (F2-130d)."""

from uuid import UUID

from apps.members import invitation_mail
from apps.organizations.services import SEND_REQUESTED
from core.outbox import subscribe
from core.outbox.models import OutboxEvent
from core.tenancy.celery import platform_task, tenant_task
from core.tenancy.context import require


@subscribe(SEND_REQUESTED)
@tenant_task(name="members.invitation_event", ignore_result=True, acks_late=True)
def invitation_event(event_id: str) -> None:
    """Handler del outbox: encola el envío con identificadores. No envía aquí: el correo sale
    después de confirmar el enlace, y este cuerpo corre dentro de la transacción de su scope."""
    event = OutboxEvent.objects.filter(id=event_id, event_type=SEND_REQUESTED).first()
    if event is not None:
        ids = {
            "organization_id": str(require().organization_id),
            "invitation_id": str(event.aggregate_id),
            "event_id": str(event_id),
        }
        send_invitation.apply_async(kwargs=ids)


# Sin reintentos y sin `acks_late`: cada ejecución puede emitir un enlace, y un enlace emitido
# cuenta para el tope de su invitación (ADR-020 §3). Una entrega repetida no envía dos veces.
@platform_task(name="members.send_invitation", ignore_result=True, max_retries=0)
def send_invitation(organization_id: str, invitation_id: str, event_id: str) -> str:
    """Emite y envía el enlace de una invitación (`invitation_mail.send`). Si la entrega falla,
    la tarea falla y no reintenta."""
    return invitation_mail.send(UUID(organization_id), UUID(invitation_id), UUID(event_id))
