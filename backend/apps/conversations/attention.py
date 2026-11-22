"""La solicitud de atención humana (F8-08n; ADR-034 §8, ADR-011): la pide quien atiende (`AGENT`)
o el copiloto al detectar un traspaso, a nombre de su persona; se cierra a mano, al responder una
persona, al tomarla o pasarla, o al resolverla o cerrarla. Idempotente; organización del contexto.
Audita ids y códigos y deja `conversation.updated` para el Inbox; nada sale al cliente."""

from datetime import datetime
from typing import Any
from uuid import UUID

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.access.selectors import AccessDenied, Denied, can, execution_context, has_permission
from apps.audit.services import Entity, record
from apps.conversations.models import Conversation, HumanRequest, Message, Status
from apps.conversations.policy import ConversationClosed
from core.outbox import emit
from core.tenancy.context import TenantContext
from core.tenancy.scope import require_scope

VIEW = "conversations.view"
FIELDS = ("human_requested_at", "human_request_reason", "human_requested_by_user_id")


def _visible(ctx: TenantContext, alias: str, conversation_id: Any) -> Conversation:
    """La conversación bloqueada, si el actor tiene `conversations.view` (antes de leerla) y está en
    su alcance; si no, el mismo `DoesNotExist` que una desconocida o de otra organización."""
    if not isinstance(conversation_id, UUID):
        raise ValueError("conversación: un identificador")
    ectx = execution_context(ctx)  # `AccessDenied` si no es un miembro activo
    if not has_permission(ectx, VIEW):
        raise AccessDenied(Denied.PERMISSION)
    found = Conversation.objects.using(alias).select_for_update(no_key=True)
    convo: Conversation | None = found.filter(pk=conversation_id).first()
    if convo is None or not can(ectx, VIEW, convo):
        raise Conversation.DoesNotExist("Conversation matching query does not exist.")
    return convo


def _write(
    ctx: TenantContext, alias: str, convo: Conversation, values: dict[str, Any], action: str,
    metadata: dict[str, str],
) -> None:  # fmt: skip
    """Solo estas columnas (quien llama puede tener la fila con otros cambios ya guardados), la
    auditoría con ids y códigos y la pista para el Inbox."""
    before = convo.human_request_reason
    Conversation.objects.using(alias).filter(pk=convo.pk).update(
        **values, updated_at=timezone.now()
    )
    for name, value in values.items():
        setattr(convo, name, value)
    changes = {"human_request_reason": [before, convo.human_request_reason]}
    record(ctx, action, Entity("conversation", convo.pk, convo.number), changes, metadata)
    emit(ctx, "conversation.updated", aggregate_type="conversation", aggregate_id=convo.pk)


def handoff_reply(text: str) -> bool:
    """¿Es `text` el texto fijo de traspaso del copiloto? Enviarlo no es atender: no cierra la
    solicitud. Hoy, `AI_HANDOFF_REPLY`; #1046 lo hace por organización y cambia esta función."""
    return text.split() == settings.AI_HANDOFF_REPLY.split()


def request_human(
    ctx: TenantContext, *, conversation_id: UUID, reason: str, ai_run_id: UUID | None = None,
    after: datetime | None = None,
) -> tuple[Conversation, bool]:  # fmt: skip
    """Pide que una persona atienda la conversación; `True` si la abrió ahora. Con una ya abierta
    la devuelve tal cual (quién, cuándo y por qué siguen siendo los primeros), y con `after` (el
    copiloto: cuándo empezó su run) tampoco abre nada si desde entonces una persona ya respondió.
    `ConversationClosed` si está cerrada; `DoesNotExist` fuera del alcance; `AccessDenied` sin el
    permiso."""
    alias = require_scope(ctx)
    if reason not in HumanRequest.values:
        raise ValueError("motivo no admitido")
    with transaction.atomic(using=alias):
        convo = _visible(ctx, alias, conversation_id)
        if convo.status == Status.CLOSED:
            raise ConversationClosed
        mine = Message.objects.using(alias).filter(conversation_id=convo.pk, sender_type="USER")
        replied = after and mine.filter(direction="OUTBOUND", created_at__gte=after).exists()
        if convo.human_requested_at is not None or replied:
            return convo, False
        values = dict(zip(FIELDS, (timezone.now(), reason, ctx.user_id), strict=True))
        metadata = {"reason": reason} | ({"ai_run_id": str(ai_run_id)} if ai_run_id else {})
        _write(ctx, alias, convo, values, "conversation.human_requested", metadata)
        return convo, True


def clear(ctx: TenantContext, alias: str, convo: Conversation, cause: str, **ids: UUID) -> None:
    """Cierra la solicitud de la conversación ya bloqueada, si la hay (`cause`: `MANUAL`,
    `REPLIED`, `ASSIGNED` o `CLOSED`: resuelta o cerrada). Para los comandos, en su transacción."""
    if convo.human_requested_at is not None:
        metadata = {"cause": cause} | {name: str(value) for name, value in ids.items()}
        values = dict.fromkeys(FIELDS)
        _write(ctx, alias, convo, values, "conversation.human_request_closed", metadata)


def close_request(ctx: TenantContext, *, conversation_id: UUID) -> tuple[Conversation, bool]:
    """Cierra a mano la solicitud; `True` si había una. Sin solicitud, nada cambia."""
    alias = require_scope(ctx)
    with transaction.atomic(using=alias):
        convo = _visible(ctx, alias, conversation_id)
        opened = convo.human_requested_at is not None
        clear(ctx, alias, convo, "MANUAL")
        return convo, opened
