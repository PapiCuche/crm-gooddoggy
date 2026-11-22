"""F8-12b (#1068; ADR-045, ADR-034 §5, ADR-011): el aviso de los dos eventos del presupuesto de IA.
Lo que hace la tarea `ai_gateway.send_budget_alert`: en el `tenant_scope` de la organización del
evento (escrito bajo RLS), con actor `SYSTEM`, relee el evento, bloquea el tope y, si su mes tiene
la marca del aviso y la auditoría no tiene ya ese aviso, deja una fila `ai.budget.alerted` y
confirma. Después escribe a cada miembro activo con `ai.manage`, por el correo de siempre
(`core.mail`). Una vez por aviso, mes y organización: otra entrega del mismo evento, o un evento
falso del mismo mes (ADR-045: el runtime puede escribirlo), no repite. Un evento sin la marca de su
mes no avisa. El correo no lleva importes ni datos del proveedor, y su único enlace es
`/o/<slug>/ia`. Sin servidor de correo, la fila queda igual y no se escribe a nadie. Si una entrega
falla, las demás siguen y nada se reintenta: el aviso ya consta y no se repite.
"""

from datetime import UTC, date, datetime, time
from typing import Any
from uuid import UUID

from django.conf import settings
from django.db import DatabaseError

from apps.access.selectors import holders
from apps.ai_gateway.models import AIBudget, AIBudgetUsage
from apps.audit.models import AuditLog
from apps.audit.services import Entity, record
from apps.organizations.models import Organization
from core import mail
from core.observability.logging import get_logger
from core.outbox.models import OutboxEvent
from core.tenancy.context import ActorType, TenantContext
from core.tenancy.resolution import ACCESSIBLE_STATUSES
from core.tenancy.scope import require_scope, tenant_scope

logger = get_logger(__name__)
THRESHOLD, EXCEEDED = "ai.budget.threshold_reached", "ai.budget.exceeded"
EVENTS = (THRESHOLD, EXCEEDED)
ACTION, MANAGE, LINK_PATH = "ai.budget.alerted", "ai.manage", "/o/{slug}/ia"
MARKS = {THRESHOLD: "threshold_alerted_at", EXCEEDED: "exceeded_alerted_at"}
MONTHS = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
          "septiembre", "octubre", "noviembre", "diciembre")  # fmt: skip
SUBJECTS = {  # fijos: nada que escriba una organización
    THRESHOLD: "El gasto de IA llegó al 80 % del tope del mes",
    EXCEEDED: "El gasto de IA llegó al tope del mes",
}
NEWS = {  # una línea cada párrafo: el programa de correo de quien lee lo ajusta
    THRESHOLD: "llegó al 80 % del tope mensual de {month}.\n\nAl llegar al tope, el copiloto deja "
    "de responder hasta el mes siguiente, salvo que subas el tope.",
    EXCEEDED: "llegó al tope mensual de {month}.\n\nEl copiloto no responderá hasta el mes "
    "siguiente, salvo que subas el tope.",
}
BODY = (
    "Hola:\n\nEl gasto del copiloto de IA de «{organization}» {news}\n\n"
    "Revisa la configuración de la IA en:\n\n{link}\n\n"
    "Recibes este correo porque administras la IA de esta organización. Cada aviso se envía una "
    "sola vez al mes.\n"
)
Found = tuple[str, str, str, date, list[str]]  # evento, slug, organización, mes y correos


class NotRecorded(Exception):
    """La base falló antes de confirmar la fila: no se avisó a nadie y se puede reintentar."""


def _month(payload: Any) -> date | None:
    value = payload.get("month") if isinstance(payload, dict) else None
    try:
        month = date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None
    return month if month is not None and month.day == 1 else None


def _record(ctx: TenantContext, event_id: UUID) -> Found | str:
    """Dentro del scope: comprueba, bloquea el tope y audita. Devuelve lo que lleva el correo, o
    la razón por la que no se avisa."""
    alias = require_scope(ctx)
    events = OutboxEvent.objects.using(alias).filter(
        pk=event_id, event_type__in=EVENTS, aggregate_type="ai_budget"
    )
    event = events.values_list("event_type", "aggregate_id", "payload").first()
    if event is None:
        return "event"
    kind, budget_id, payload = event
    month = _month(payload)
    if month is None:
        return "payload"
    # El tope bloqueado ordena dos entregas a la vez: la segunda ya ve la fila de la primera.
    budgets = AIBudget.objects.using(alias).select_for_update(no_key=True).filter(pk=budget_id)
    if budgets.values_list("pk", flat=True).first() is None:
        return "budget"
    marked = {"month": month, f"{MARKS[kind]}__isnull": False}
    if not AIBudgetUsage.objects.using(alias).filter(**marked).exists():
        return "unmarked"
    since = datetime.combine(month, time.min, tzinfo=UTC)  # antes del día 1 en Lima
    alerted = AuditLog.objects.using(alias).filter(
        occurred_at__gte=since, action=ACTION, entity_id=budget_id
    )
    if alerted.filter(metadata__event=kind, metadata__month=month.isoformat()).exists():
        return "duplicate"
    organizations = Organization.objects.using(alias).filter(
        pk=ctx.organization_id, status__in=ACCESSIBLE_STATUSES
    )
    organization = organizations.values_list("slug", "name").first()
    if organization is None:
        return "organization"
    to = [email for _, email in holders(ctx, MANAGE) if mail.deliverable(email)]
    metadata = {"event": kind, "month": month.isoformat(), "recipients": len(to)}
    record(ctx, ACTION, Entity("ai_budget", budget_id), metadata=metadata)
    return kind, *organization, month, to


def send(organization_id: UUID, event_id: UUID) -> str:
    """Audita y envía el aviso del evento `event_id`. Devuelve `sent`, `unsent` (sin correo
    configurado) o la razón por la que no avisa (ver `_record`). Un fallo de la base antes de
    confirmar es `NotRecorded`; una entrega fallida solo se registra."""
    ctx = TenantContext(organization_id, "celery", actor_type=ActorType.SYSTEM)
    try:
        with tenant_scope(ctx):
            found = _record(ctx, event_id)
    except DatabaseError as error:
        raise NotRecorded(type(error).__name__) from None
    ids = {"organization_id": str(organization_id), "event_id": str(event_id)}
    if isinstance(found, str):
        logger.info("ai.budget_alert_skipped", reason=found, **ids)
        return found
    kind, slug, name, month, recipients = found  # confirmado: ahora sí, el correo
    if not settings.APP_ORIGIN or not mail.configured():
        logger.warning("ai.budget_alert_unsent", recipients=len(recipients), **ids)
        return "unsent"
    news = NEWS[kind].format(month=f"{MONTHS[month.month - 1]} de {month.year}")
    link = settings.APP_ORIGIN + LINK_PATH.format(slug=slug)
    body = BODY.format(organization=name, news=news, link=link)
    failed = 0
    for to in recipients:
        try:
            mail.send(mail.Message(to=to, subject=SUBJECTS[kind], body=body, purpose="ai_budget"))
        except mail.MailError, ValueError:  # solo el tipo, en `core.mail`; las demás siguen
            failed += 1
    logger.info("ai.budget_alert_sent", sent=len(recipients) - failed, failed=failed, **ids)
    return "sent"
