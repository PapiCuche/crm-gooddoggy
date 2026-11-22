"""La solicitud de atención humana (F8-08n; ADR-034 §8, ADR-011): la pide quien atiende (`AGENT`)
o el copiloto al detectar un traspaso, a nombre de su persona; se cierra a mano, al responder una
persona, al tomarla o pasarla, o al resolverla o cerrarla. Idempotente; organización del contexto.
Audita ids y códigos y deja `conversation.updated` para el Inbox; nada sale al cliente.
También la exclusión de la IA de una conversación (F8-13, `set_ai_excluded`) y su lectura con
candado compartido para el copiloto (F8-13c, `ai_included`)."""

from datetime import datetime
from typing import Any
from uuid import UUID

from django.db import connections, transaction
from django.db.models import F, QuerySet
from django.utils import timezone

from apps.access.selectors import AccessDenied, Denied, can, execution_context, has_permission
from apps.audit.services import Entity, record
from apps.conversations import handoff
from apps.conversations.models import Conversation, HumanRequest, Message, Status
from apps.conversations.policy import ConversationClosed
from core.outbox import emit
from core.tenancy.context import TenantContext
from core.tenancy.scope import require_scope

VIEW = "conversations.view"
MANAGE_AI = "ai.manage"  # F8-13: volver a incluirla en la IA
# F8-13c: compartido; `_visible` bloquea con FOR NO KEY UPDATE, que espera a este (y este, a él)
INCLUDED = "SELECT ai_excluded_at IS NULL FROM conversations WHERE id = %s FOR SHARE"
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


def handoff_reply(ctx: TenantContext, text: str) -> bool:
    """¿Es `text` el texto de traspaso vigente de la organización de `ctx` (`handoff`), salvo
    espacios? Enviarlo no es atender: no cierra la solicitud ni cuenta como respuesta. Se compara
    con el de ahora: uno anterior, cambiado después, sí cuenta (F8-08l2, #1058)."""
    return not _answered(ctx, [text])


def _answered(ctx: TenantContext, bodies: list[str]) -> bool:
    """¿Alguno de `bodies` no es el texto de traspaso? Sin ninguno, no lo lee."""
    if not bodies:
        return False
    fixed = handoff.text(ctx).split()
    return any(body.split() != fixed for body in bodies)


def _late(ctx: TenantContext, mine: QuerySet[Message], after: datetime | None) -> bool:
    """¿Respondió una persona al cliente desde `after`? Con su texto de traspaso, no; ni con una
    nota o un mensaje del sistema."""
    if after is None:
        return False
    late = mine.filter(direction="OUTBOUND", created_at__gte=after)
    return _answered(ctx, list(late.values_list("body", flat=True)))


def request_human(
    ctx: TenantContext, *, conversation_id: UUID, reason: str, ai_run_id: UUID | None = None,
    after: datetime | None = None,
) -> tuple[Conversation, bool]:  # fmt: skip
    """Pide que una persona atienda la conversación; `True` si la abrió ahora. Con una ya abierta
    la devuelve tal cual (quién, cuándo y por qué siguen siendo los primeros), y con `after` (el
    copiloto: cuándo empezó su run) tampoco abre nada si desde entonces una persona ya respondió
    (con algo que no sea el texto de traspaso).
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
        if convo.human_requested_at is not None or _late(ctx, mine, after):
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


class VersionConflict(Exception):  # noqa: N818 — la versión enviada ya no es la de la fila
    pass


def set_ai_excluded(
    ctx: TenantContext, *, conversation_id: UUID, excluded: bool, version: int
) -> tuple[Conversation, bool]:
    """Excluye la conversación de la IA (F8-13) o la vuelve a incluir; `True` si cambió. Excluir
    pide `conversations.view` y su alcance (protege: quien atiende cumple lo que pide el cliente);
    incluirla, además `ai.manage` (el segundo factor lo pide la ruta). Permiso, bloqueo, alcance
    sobre la fila bloqueada (si no, el mismo `DoesNotExist`), versión (`VersionConflict`) y estado:
    la misma marca no cambia nada. Audita solo ids y deja `conversation.updated` para el Inbox."""
    if type(excluded) is not bool or type(version) is not int or not 1 <= version <= 2**31 - 1:
        raise ValueError("exclusión: un booleano y una versión desde 1")
    alias = require_scope(ctx)
    with transaction.atomic(using=alias):
        if not excluded and not has_permission(execution_context(ctx), MANAGE_AI):
            raise AccessDenied(Denied.PERMISSION)
        convo = _visible(ctx, alias, conversation_id)
        if convo.version != version:
            raise VersionConflict
        if (convo.ai_excluded_at is not None) is excluded:
            return convo, False
        Conversation.objects.using(alias).filter(pk=convo.pk).update(
            ai_excluded_at=timezone.now() if excluded else None,
            ai_excluded_by_user_id=ctx.user_id if excluded else None,
            version=F("version") + 1, updated_at=timezone.now(),
        )  # fmt: skip
        action = "conversation.ai_excluded" if excluded else "conversation.ai_included"
        changes = {"ai_excluded": [not excluded, excluded]}
        record(ctx, action, Entity("conversation", convo.pk, convo.number), changes)
        emit(ctx, "conversation.updated", aggregate_type="conversation", aggregate_id=convo.pk)
        convo.refresh_from_db(using=alias)
        return convo, True


def ai_included(ctx: TenantContext, conversation_id: UUID) -> bool:
    """F8-13c: si la conversación (RLS) no está excluida de la IA, con su fila bloqueada en modo
    compartido hasta el fin de la transacción activa de `ctx`: `set_ai_excluded` espera a que
    termine (la reserva del copiloto) o, si ya la bloqueó, esta espera a ver su marca. Sin fila,
    `False` (falla cerrado). Sin permisos: quien llama ya los miró."""
    with connections[require_scope(ctx)].cursor() as cursor:
        cursor.execute(INCLUDED, [conversation_id])
        row = cursor.fetchone()
    return row is not None and row[0] is True
