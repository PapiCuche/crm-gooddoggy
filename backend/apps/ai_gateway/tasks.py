"""Tareas de `ai_gateway` (F8-03a): el re-cifrado de las claves de proveedor tras rotar la clave
maestra (ADR-022 §5), el barrido de reservas huérfanas (F8-05b5) y los avisos del presupuesto
(F8-12b)."""

from uuid import UUID

from apps.ai_gateway import alerts, keys, orphans
from core.outbox import subscribe
from core.outbox.models import OutboxEvent
from core.tenancy.celery import platform_task, tenant_task
from core.tenancy.context import require


@platform_task(name="ai_gateway.reseal_provider_keys")
def reseal_provider_keys(max_orgs: int = 50, batch_size: int = 100) -> dict[str, int]:
    """Vuelve a cifrar con la clave vigente las claves de proveedor que quedaron con otra.
    Devuelve la clave vigente y cuántas volvió a cifrar, no pudo abrir o dejó para otra pasada."""
    return keys.reseal(max_orgs=max_orgs, batch_size=batch_size)


@platform_task(name="ai_gateway.close_orphan_reservations", ignore_result=True)
def close_orphan_reservations(max_orgs: int = 50, batch_size: int = 100) -> dict[str, int]:
    """Cierra como gastadas las reservas de IA huérfanas (ADR-034 §5): sin tenant al empezar; el
    de cada una sale de la base. Devuelve solo cuentas."""
    return orphans.close(max_orgs=max_orgs, batch_size=batch_size)


@subscribe(*alerts.EVENTS)
# Antes de encolar nada: un fallo (la base, el broker) se reintenta, y el aviso no se repite.
@tenant_task(
    name="ai_gateway.budget_alert_event",
    ignore_result=True,
    acks_late=True,
    autoretry_for=(Exception,),
    max_retries=3,
    retry_backoff=30,
    retry_backoff_max=120,
)
def budget_alert_event(event_id: str) -> None:
    """Handler del outbox: encola el aviso con identificadores. No avisa aquí: el correo sale
    después de confirmar la auditoría, y este cuerpo corre dentro de la transacción de su scope."""
    if OutboxEvent.objects.filter(id=event_id, event_type__in=alerts.EVENTS).exists():
        ids = {"organization_id": str(require().organization_id), "event_id": str(event_id)}
        send_budget_alert.apply_async(kwargs=ids)


# Sin `acks_late` y sin reintentos después de confirmar la fila: el aviso ya consta y no se repite.
@platform_task(
    name="ai_gateway.send_budget_alert",
    ignore_result=True,
    autoretry_for=(alerts.NotRecorded,),
    max_retries=3,
    retry_backoff=30,
    retry_backoff_max=120,
)
def send_budget_alert(organization_id: str, event_id: str) -> str:
    """Audita y envía el aviso de gasto de IA a quienes tienen `ai.manage` (`alerts.send`)."""
    return alerts.send(UUID(organization_id), UUID(event_id))
