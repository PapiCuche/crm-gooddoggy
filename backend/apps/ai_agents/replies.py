"""El texto de traspaso de cada organización (F8-08l, #1046; ADR-034 §8; decisión del propietario
del 2026-11-21 en #771): lo que el copiloto propone cuando el cliente pide una persona. Sin texto
guardado, el inicial (`AI_HANDOFF_REPLY`). Leerlo y cambiarlo piden `ai.manage` antes de nada (el
segundo factor reciente lo pone la ruta y el tope de escrituras, el middleware, como en las
cuentas); el cambio se audita (`ai.handoff_reply.changed`) con su longitud, nunca con el texto.

Se valida al guardar y al usarlo (`detectors.reply_problems`): uno inválido no se guarda, y uno
guardado que ya no vale (los detectores cambiaron después) no se propone: se propone el inicial y
el log lo avisa con los códigos, sin el texto. La organización, siempre la del contexto."""

from dataclasses import dataclass
from datetime import datetime

from django.db import connections, transaction
from django.utils import timezone

from apps.access.selectors import AccessDenied, Denied, execution_context, has_permission
from apps.ai_agents import detectors
from apps.ai_agents.models import AIHandoffReply
from apps.audit.services import Entity, record
from core.observability.logging import get_logger
from core.tenancy.context import TenantContext
from core.tenancy.scope import require_scope, tenant_scope

logger = get_logger(__name__)
ACTION = "ai.handoff_reply.changed"
# La fila de la organización con el texto pedido; si ya existe (o otra petición la crea a la vez),
# nada: entonces se bloquea y se cambia.
_CREATE = (
    "INSERT INTO public.ai_handoff_replies (organization_id, text, changed_by_user_id, changed_at)"
    " VALUES (%s, %s, %s, now()) ON CONFLICT (organization_id) DO NOTHING RETURNING id"
)


class InvalidReply(ValueError):  # noqa: N818 — lleva solo los códigos, nunca el texto
    def __init__(self, problems: tuple[str, ...]) -> None:
        super().__init__(*problems)
        self.problems = problems


@dataclass(frozen=True)
class Reply:
    text: str
    initial: bool  # el del propietario: sin texto guardado, o el guardado ya no vale
    changed_at: datetime | None


def _allowed(ctx: TenantContext) -> None:
    ectx = execution_context(ctx)  # `AccessDenied` si no es un miembro activo
    if not has_permission(ectx, "ai.manage"):
        raise AccessDenied(Denied.PERMISSION)


def _effective(alias: str) -> Reply:
    """El que propone el copiloto, validado al usarlo."""
    row: AIHandoffReply | None = AIHandoffReply.objects.using(alias).first()
    if row is None:
        return Reply(detectors.handoff_reply(), True, None)
    if problems := detectors.reply_problems(row.text):
        logger.warning("ai.handoff_reply_invalid", problems=list(problems))
        return Reply(detectors.handoff_reply(), True, row.changed_at)
    return Reply(row.text, False, row.changed_at)


def current(ctx: TenantContext) -> str:
    """El texto de traspaso de la organización de `ctx`, en su propio scope corto. Sin permiso:
    lo usa el runtime del copiloto, que ya comprobó `ai.use`."""
    with tenant_scope(ctx):
        return _effective(require_scope(ctx)).text


def read(ctx: TenantContext) -> Reply:
    """El texto que propone el copiloto. `AccessDenied` sin `ai.manage`."""
    alias = require_scope(ctx)
    _allowed(ctx)
    return _effective(alias)


def change(ctx: TenantContext, text: str) -> Reply:
    """Guarda el texto de traspaso de la organización y lo audita con el antes y el después de su
    longitud. Guardar el primero se audita siempre (la organización deja el inicial, aunque sea
    igual); volver a guardar el mismo no cambia nada ni se audita. `AccessDenied` sin `ai.manage`
    (antes de mirar el texto); `InvalidReply`."""
    alias = require_scope(ctx)
    _allowed(ctx)
    if problems := detectors.reply_problems(text):
        raise InvalidReply(problems)
    with transaction.atomic(using=alias):
        before = _effective(alias).text
        with connections[alias].cursor() as cursor:
            cursor.execute(_CREATE, [ctx.organization_id, text, ctx.user_id])
            created = cursor.fetchone()
        rows = AIHandoffReply.objects.using(alias)
        row = rows.select_for_update().get()
        if created is None and row.text != text:
            before = row.text
            rows.filter(pk=row.pk).update(
                text=text, changed_by_user_id=ctx.user_id, changed_at=timezone.now()
            )
        if created is not None or before != text:
            changes = {"length": [len(before), len(text)]}
            record(ctx, ACTION, Entity("ai_handoff_reply", row.pk), changes)
        done = _effective(alias)
    return done
