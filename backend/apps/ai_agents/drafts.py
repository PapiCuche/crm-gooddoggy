"""Insertar una sugerencia y purgar el texto de los borradores (F8-08j; ADR-034 §9.2, §13 D-F8-05).

`insert` corre en la transacción de la petición (una ruta de tenant normal: no llama al modelo).
Solo una sugerencia (`SUGGEST`) con texto, **pedida por la misma persona** (sus tools leyeron con
sus permisos: otra persona no obtiene un precio o un stock que su propia sugerencia marcaría), de
una conversación que ve con su alcance; si no, `NotFound` (el mismo 404). Con su evidencia
caducada, `EvidenceExpired`, sin auditar. Si no, audita `ai.suggestion.inserted`
(`records.inserted`) y devuelve el texto guardado, con sus marcas: nada se envía ni va al outbox.
Cada inserción se audita: la métrica de sugerencias insertadas cuenta ids distintos.

`purge` (la tarea diaria): un scope por organización, como `crm_app` bajo RLS, pone a NULL el texto
de los borradores y resúmenes de más de `AI_SUGGESTION_TEXT_DAYS` días. El log, solo cuentas."""

from collections import Counter
from uuid import UUID

from django.conf import settings
from django.db.models import BooleanField, ExpressionWrapper, Q
from django.db.models.functions import Now

from apps.access.selectors import AccessDenied, execution_context, scoped
from apps.ai_agents import records
from apps.ai_agents.models import AISuggestion
from apps.conversations.models import Conversation
from apps.organizations.models import Organization
from core.observability.logging import get_logger
from core.tenancy.context import TenantContext

logger = get_logger(__name__)
BATCH = 500


class NotFound(Exception):  # noqa: N818 — ajena, inexistente, de otra conversación, resumen o purgada
    pass


class EvidenceExpired(Exception):  # noqa: N818 — el panel pide regenerar
    pass


def insert(ctx: TenantContext, conversation_id: UUID, suggestion_id: UUID) -> str:
    """El texto insertable de la sugerencia, auditado en la transacción de quien llama."""
    try:
        ectx = execution_context(ctx)
    except AccessDenied:
        raise NotFound from None
    mine = scoped(ectx, "conversations.view", Conversation.objects.filter(pk=conversation_id))
    expired = ExpressionWrapper(Q(evidence_expires_at__lt=Now()), BooleanField())
    found = (  # solo una sugerencia suya, con texto, de esta conversación, en su alcance
        AISuggestion.objects.filter(
            pk=suggestion_id,
            kind="SUGGEST",
            text__isnull=False,
            conversation_id__in=mine,
            requested_by_user_id=ctx.user_id,
        )
        .annotate(expired=expired)
        .values_list("text", "findings", "expired")
        .first()
    )
    if found is None:
        raise NotFound
    text, findings, stale = found
    if stale:
        raise EvidenceExpired
    counts = Counter(str(finding["type"]) for finding in findings)
    records.inserted(ctx, suggestion_id, conversation_id, counts)
    return str(text)


def purge(batch: int = BATCH) -> dict[str, int]:
    """Purga cada organización en su scope; un fallo no detiene a las demás."""
    done = Counter(organizations=0, purged=0, failed=0)
    days = settings.AI_SUGGESTION_TEXT_DAYS
    for organization_id in list(Organization.objects.order_by("pk").values_list("pk", flat=True)):
        done["organizations"] += 1
        ctx = TenantContext(organization_id, "celery")
        try:
            while (purged := records.purge_text(ctx, days, batch)) > 0:
                done["purged"] += purged
                if purged < batch:
                    break
        except Exception as error:  # noqa: BLE001 — una organización no detiene a las demás
            logger.error("ai.suggestion_text_purge_failed", organization_id=str(organization_id),
                         error=type(error).__name__)  # fmt: skip
            done["failed"] += 1
    logger.info("ai.suggestion_text_purged", **done)
    return dict(done)
