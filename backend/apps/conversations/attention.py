"""La solicitud de atención humana (F8-08n; ADR-034 §8, ADR-011): la pide quien atiende (`AGENT`)
o el copiloto al detectar un traspaso, a nombre de su persona; se cierra a mano, al responder una
persona, al tomarla o pasarla, o al resolverla o cerrarla. Idempotente; organización del contexto.
Audita ids y códigos y deja `conversation.updated` para el Inbox; nada sale al cliente.
También la exclusión de la IA de una conversación (F8-13, `set_ai_excluded`) y, para el copiloto
(F8-13c), el candado de cada llamada (`ai_call`) y su comprobación (`ai_included`)."""

import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any
from uuid import UUID

from django.conf import settings
from django.db import OperationalError, connections, transaction
from django.db.models import F, QuerySet
from django.utils import timezone

from apps.access.selectors import AccessDenied, Denied, can, execution_context, has_permission
from apps.audit.services import Entity, record
from apps.conversations import handoff
from apps.conversations.models import Conversation, HumanRequest, Message, Status
from apps.conversations.policy import ConversationClosed
from core.db.timeouts import ms as _ms
from core.outbox import emit
from core.tenancy.context import TenantContext
from core.tenancy.scope import require_scope

VIEW = "conversations.view"
MANAGE_AI = "ai.manage"  # F8-13: volver a incluirla en la IA
# F8-13c: el candado de las llamadas a la IA de una conversación: compartido de sesión en cada una
# (`ai_call`), exclusivo de transacción al excluirla. Siempre el consultivo y después la fila.
CALL, WAITER = "ai-call:{}:{}", "ai-call-wait:{}:{}"  # el segundo: como mucho una exclusión espera
_KEY = "hashtextextended(%s, 0)"
_CALL_SHARED, _CALL_FREE = (f"SELECT pg_advisory_lock_shared({_KEY})",
                            f"SELECT pg_advisory_unlock_shared({_KEY})")  # fmt: skip
_CALL_EXCLUSIVE, _ONE = (f"SELECT pg_advisory_xact_lock({_KEY})",
                         f"SELECT pg_try_advisory_xact_lock({_KEY})")  # fmt: skip
_WAIT = "SELECT set_config('lock_timeout', %s, true)"
# La marca y si esta sesión tiene el candado (falla cerrado). Sin `FOR SHARE`: con el candado, la
# marca no cambia. Una clave `bigint` va en `classid` (alta) y `objid` (baja).
INCLUDED = """
WITH k AS (SELECT hashtextextended(%s, 0) AS v)
SELECT c.ai_excluded_at IS NULL, EXISTS (
  SELECT 1 FROM pg_locks l, k WHERE l.locktype = 'advisory' AND l.pid = pg_backend_pid()
    AND l.granted AND l.objsubid = 1
    AND l.classid = ((k.v >> 32) & 4294967295)::oid AND l.objid = (k.v & 4294967295)::oid)
FROM conversations c WHERE c.id = %s
"""


class AICallInProgress(Exception):  # noqa: N818 — una llamada a la IA no terminó a tiempo
    pass


class CallLockLost(Exception):  # noqa: N818 — F8-13d: la sesión ya no tiene el candado de la llamada
    pass


class ConversationBusy(Exception):  # noqa: N818 — F8-13d: la fila siguió bloqueada todo el plazo
    pass


FIELDS = ("human_requested_at", "human_request_reason", "human_requested_by_user_id")


def _visible(
    ctx: TenantContext, alias: str, conversation_id: Any, *, lock: bool = True
) -> Conversation:
    """La conversación (bloqueada si `lock`), si el actor tiene `conversations.view` (antes de
    leerla) y está en su alcance; si no, el mismo `DoesNotExist` que una desconocida o ajena."""
    if not isinstance(conversation_id, UUID):
        raise ValueError("conversación: un identificador")
    ectx = execution_context(ctx)  # `AccessDenied` si no es un miembro activo
    if not has_permission(ectx, VIEW):
        raise AccessDenied(Denied.PERMISSION)
    found = Conversation.objects.using(alias)
    found = found.select_for_update(no_key=True) if lock else found
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
    la misma marca no cambia nada. Audita solo ids y deja `conversation.updated` para el Inbox.
    F8-13d: los dos caminos miran alcance y versión antes de esperar; la fila, si sigue bloqueada
    todo el plazo, es `ConversationBusy` (nada cambió)."""
    if type(excluded) is not bool or type(version) is not int or not 1 <= version <= 2**31 - 1:
        raise ValueError("exclusión: un booleano y una versión desde 1")
    alias = require_scope(ctx)
    with transaction.atomic(using=alias):
        if not excluded and not has_permission(execution_context(ctx), MANAGE_AI):
            raise AccessDenied(Denied.PERMISSION)
        # alcance y versión antes de esperar (el mismo 404, sin esperar); excluir espera además a
        # las llamadas (F8-13c); incluir, solo a la fila, con el mismo tope (F8-13d)
        if _visible(ctx, alias, conversation_id, lock=False).version != version:
            raise VersionConflict
        if excluded:
            _wait_for_calls(ctx, alias, conversation_id)
        else:
            with connections[alias].cursor() as cursor:
                cursor.execute(_WAIT, [_ms(settings.AI_EXCLUSION_WAIT_SECONDS)])
        with _busy(ConversationBusy):
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


def _key(ctx: TenantContext, conversation_id: UUID) -> str:
    return CALL.format(ctx.organization_id, conversation_id)


@contextmanager
def _busy(busy: type[Exception]) -> Iterator[None]:
    """Un `lock_timeout` agotado (55P03) es `busy`; otro error sale tal cual."""
    try:
        yield
    except OperationalError as error:
        if getattr(error.__cause__, "sqlstate", None) == "55P03":  # lock_not_available
            raise busy from None
        raise


def _locked(cursor: Any, sql: str, key: str) -> None:
    """Un candado con el `lock_timeout` ya fijado; si no llega, `AICallInProgress`."""
    with _busy(AICallInProgress):
        cursor.execute(sql, [key])


def _wait_for_calls(ctx: TenantContext, alias: str, conversation_id: UUID) -> None:
    """El candado exclusivo de las llamadas, como mucho `AI_EXCLUSION_WAIT_SECONDS`, y una sola
    exclusión esperando por conversación (otra, `AICallInProgress` enseguida: no retiene una
    conexión). A la fila le queda solo lo que sobre del plazo (F8-13d): nunca dos esperas."""
    key, end = _key(ctx, conversation_id), time.monotonic() + settings.AI_EXCLUSION_WAIT_SECONDS
    with connections[alias].cursor() as cursor:
        cursor.execute(_WAIT, [_ms(settings.AI_EXCLUSION_WAIT_SECONDS)])
        for one in (key, WAITER.format(ctx.organization_id, conversation_id)):
            cursor.execute(_ONE, [one])  # libre: sin esperar; si no, ¿hay ya quien espera?
            if cursor.fetchone()[0] is True:
                break
        else:
            raise AICallInProgress
        if one != key:
            _locked(cursor, _CALL_EXCLUSIVE, key)
        cursor.execute(_WAIT, [_ms(end - time.monotonic())])


@contextmanager
def ai_call(ctx: TenantContext, conversation_id: UUID, seconds: float) -> Iterator[None]:
    """F8-13c: el candado compartido de una llamada a la IA sobre la conversación, de sesión, en la
    conexión de `ctx` y **fuera** de toda transacción: de antes de la reserva al fin del envío (y
    sus reintentos). Espera como mucho `seconds` (si no, `AICallInProgress`); se toma en una
    transacción corta (el `lock_timeout` es suyo) y sigue tras ella. Se suelta al salir, también
    con un error; si no se puede soltar, se cierra la conexión (la base suelta sus candados). Si la
    transacción corta falla tras tomarlo, también se cierra (F8-13d)."""
    db = connections["default"]
    if db.in_atomic_block:
        raise RuntimeError("ai_call() es de sesión: ninguna transacción abierta")
    key = _key(ctx, conversation_id)
    try:
        with transaction.atomic(using="default"), db.cursor() as cursor:
            cursor.execute(_WAIT, [_ms(seconds)])
            _locked(cursor, _CALL_SHARED, key)
    except AICallInProgress:  # no llegó a tomarlo
        raise
    except BaseException:
        db.close()
        raise
    try:
        yield
    finally:
        try:
            with db.cursor() as cursor:
                cursor.execute(_CALL_FREE, [key])
                freed = cursor.fetchone()[0] is True
        except Exception:
            freed = False
        if not freed:
            db.close()


def ai_included(ctx: TenantContext, conversation_id: UUID) -> bool:
    """F8-13c: si la conversación (RLS) no está excluida de la IA **y** esta sesión tiene el
    candado de la llamada (`ai_call`): con él, ninguna exclusión se confirma hasta soltarlo. Sin
    fila o con la marca, `False`; incluida pero sin el candado (la sesión lo perdió, p. ej., al
    volver a conectar), `CallLockLost` (F8-13d: un fallo, no una exclusión). Sin permisos: quien
    llama ya los miró."""
    key = _key(ctx, conversation_id)
    with connections[require_scope(ctx)].cursor() as cursor:
        cursor.execute(INCLUDED, [key, conversation_id])
        row = cursor.fetchone()
    if row is None or row[0] is not True:
        return False
    if row[1] is not True:
        raise CallLockLost
    return True
