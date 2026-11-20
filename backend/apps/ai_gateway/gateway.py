"""El único punto por el que pasa toda llamada a un modelo (F8-05; ADR-034 §2, §5): abre la clave
solo en memoria, **reserva** la cota superior del costo con `ai_budget_reserve` en su propia
transacción, confirmada antes de llamar; llama fuera de toda transacción y **ajusta** la reserva una
sola vez con `ai_budget_settle` en otra, que escribe el uso real en `ai_llm_calls` (de solo
inserción; nunca el prompt ni la respuesta). Las dos funciones de la base (F8-05b1, #942) son lo
único que cambia el registro: toman la organización y quien pide de la sesión, nunca de aquí. Solo
libera si la petición no salió o hubo una respuesta de error; lo dudoso cuenta entero. El mes y el
día son los de Lima (ADR-043); sin tope mensual o diario no se llama (F8-05b2). Si el tope del mes
corta, la función devuelve NULL para que su aviso se confirme (F8-05b4) y no se llama. Sin permisos
de negocio: quien pide tiene que ser un miembro activo. El modelo es el de la cuenta (F8-05b6),
leído una vez en la transacción de la reserva: con él se estima, se reserva, se llama y se cobra;
uno sin precio (fuera de `AI_MODELS`) falla cerrado antes de abrir la clave y de reservar. Un turno
con tools (F8-05b7) se valida antes de todo; la cota suma lo que sus bytes no acotan: el
razonamiento reenviado y las imágenes (`turns.surplus`). Kill switch (F8-06, `AI_DISABLED`): el
global antes de validar; el de la organización, primero en el scope. El ajuste no lo mira."""

import time
from dataclasses import replace
from decimal import Decimal
from typing import Any, NoReturn
from uuid import UUID

from django.conf import settings
from django.db import DatabaseError, connections

from apps.access.selectors import AccessDenied, Denied
from apps.ai_gateway import keys, prices, switch, turns
from apps.ai_gateway.anthropic import AnthropicAdapter
from apps.ai_gateway.llm import (
    LLMProviderAdapter,
    LLMRequest,
    LLMResponse,
    Outcome,
    ProviderError,
    ToolCall,
)
from apps.ai_gateway.models import AccountStatus, AIProviderAccount
from core import sealed
from core.observability.logging import get_logger
from core.tenancy.context import TenantContext
from core.tenancy.scope import require_scope, tenant_scope

logger = get_logger(__name__)
ADAPTERS: dict[str, LLMProviderAdapter] = {"ANTHROPIC": AnthropicAdapter()}
RESERVE = "SELECT public.ai_budget_reserve(%s, %s, %s, %s, %s)"
SETTLE = "SELECT public.ai_budget_settle(%s, %s, %s, %s, %s, %s, %s, %s, %s)"
REFUSED = {  # la restricción con la que `ai_budget_reserve` se niega -> el código
    "ai_budget_not_set": Outcome.AI_BUDGET_NOT_SET,
    "ai_budget_exhausted": Outcome.AI_BUDGET_EXHAUSTED,
}


class GatewayError(Exception):  # noqa: N818 — solo lleva el código
    def __init__(self, outcome: Outcome, reservation_id: UUID | None = None) -> None:
        super().__init__(outcome.value)
        self.outcome = outcome
        self.reservation_id = reservation_id  # F8-08g: la de la llamada, si llegó a reservar


def _refuse(rule: str) -> NoReturn:
    """Un turno inválido o sin cota: solo el código de la regla, nunca nada del turno."""
    logger.warning("ai.turn_rejected", rule=rule)
    raise GatewayError(Outcome.AI_TURN_INVALID) from None


def _model(ctx: TenantContext, provider: str) -> str:
    """El modelo de la cuenta activa de `provider` (RLS). `GatewayError` sin cuenta activa."""
    found = AIProviderAccount.objects.using(require_scope(ctx)).filter(
        provider=provider, status=AccountStatus.ACTIVE
    )
    model: str | None = found.values_list("model", flat=True).first()
    if model is None:
        raise GatewayError(Outcome.AI_NOT_CONFIGURED)
    return model


def _reserve(
    ctx: TenantContext, request: LLMRequest, amount: Decimal, provider: str, model: str
) -> UUID | None:
    """En el scope de `ctx`: su transacción se confirma al salir, antes de llamar al proveedor.
    `None` si el tope del mes la corta: el aviso se confirma con esa transacción."""
    args: list[Any] = [amount, request.purpose, provider, model, request.conversation_id]
    try:
        with connections[require_scope(ctx)].cursor() as cursor:
            cursor.execute(RESERVE, args)
            reservation_id: UUID | None = cursor.fetchone()[0]
    except DatabaseError as error:  # la función no deja nada; el scope deshace el resto
        name = getattr(getattr(error.__cause__, "diag", None), "constraint_name", None)
        if name in REFUSED:
            raise GatewayError(REFUSED[name]) from None
        if name == "ai_budget_requester_member":
            raise AccessDenied(Denied.MEMBERSHIP) from None
        raise
    return reservation_id


def settle(
    ctx: TenantContext, reservation_id: UUID, outcome: Outcome, cost: Decimal | None,
    reply: LLMResponse | None = None, started: float | None = None, request_id: str = "",
) -> bool:  # fmt: skip
    """Ajusta la reserva una sola vez y escribe la fila de `ai_llm_calls`. `OK` con su costo (> 0)
    y su uso; `PROVIDER_UNCERTAIN` sin costo ni uso (cuenta la reserva entera); un error, con costo
    0 y sin uso (la libera). La base rechaza otra combinación. `False` si ya estaba ajustada, no
    existe o es de otra organización."""
    use = reply.usage if reply else None
    tokens = (use.input_tokens, use.output_tokens, use.cache_write_5m + use.cache_write_1h,
              use.cache_read) if use else (None,) * 4  # fmt: skip
    lapse = int((time.monotonic() - started) * 1000) if started else None
    rid = reply.request_id if reply else request_id
    with tenant_scope(ctx), connections[require_scope(ctx)].cursor() as cursor:
        cursor.execute(SETTLE, [reservation_id, outcome.value, cost, *tokens, lapse, rid])
        state = cursor.fetchone()[0]
    if state is None:
        return False
    if state == "OVER":  # la cota se quedó corta: un fallo del estimador, se cobra lo real
        logger.warning("ai.reservation_exceeded", reservation_id=str(reservation_id))
    logger.info("ai.llm_call", reservation_id=str(reservation_id), outcome=outcome)
    return True


def complete(
    ctx: TenantContext, request: LLMRequest, provider: str = "ANTHROPIC",
    deadline: float | None = None,
) -> LLMResponse:  # fmt: skip
    """Una llamada al modelo con el presupuesto de la organización de `ctx`. Se llama **fuera** de
    toda transacción (abre las suyas). `GatewayError` con el código si no hay respuesta. F8-08g:
    `deadline` (`time.monotonic()`) acota el plazo del proveedor, medido justo antes de enviar; si
    ya pasó, la reserva se libera sin enviar. La respuesta y el error llevan el id de su reserva."""
    if connections["default"].in_atomic_block:
        raise RuntimeError("complete() abre sus transacciones: ninguna puede estar abierta")
    if not switch.globally_on():  # antes de todo: el respaldo del despliegue
        raise GatewayError(Outcome.AI_DISABLED)
    params, adapter = settings.AI_PROVIDER_PARAMS[provider], ADAPTERS[provider]
    try:  # antes del scope, la clave, la reserva y la llamada
        blocks = turns.check(request)
    except turns.Rejected as error:
        _refuse(error.rule)
    with tenant_scope(ctx):
        if not switch.is_on(ctx):  # bloqueada hasta confirmar la reserva: apagar la espera
            raise GatewayError(Outcome.AI_DISABLED)
        model = _model(ctx, provider)  # el mismo para la cota, la reserva, la llamada y el costo
        try:  # el cuerpo ya codificado; lo que sus bytes no acotan (razonamiento e imágenes)
            body = adapter.encode(request, {**params, "model": model})
            if len(body) > settings.AI_TURN_LIMITS["body_bytes"]:
                raise turns.Rejected("limit")
            extra = turns.surplus(blocks, params["max_tokens"])
        except turns.Rejected as error:
            _refuse(error.rule)
        except TypeError, ValueError:  # no se puede codificar (un NaN, un sustituto suelto)
            _refuse("field")
        try:  # un token nunca es menos de un byte
            amount = prices.ceiling(provider, model, len(body) + settings.AI_TOOL_TOKENS + extra,
                                    params["max_tokens"])  # fmt: skip
        except prices.Unpriced:
            raise GatewayError(Outcome.AI_NOT_CONFIGURED) from None
        try:
            key = keys.open_key(ctx, provider)  # solo en memoria, para esta llamada
        except sealed.SealError:
            raise GatewayError(Outcome.AI_NOT_CONFIGURED) from None
        held = _reserve(ctx, request, amount, provider, model)
    if held is None:  # fuera del scope: el aviso del corte ya está confirmado
        raise GatewayError(Outcome.AI_BUDGET_EXHAUSTED)
    started = time.monotonic()  # la reserva ya está confirmada: no hay transacción abierta
    seconds = float(params["seconds"])
    if deadline is not None:
        seconds = min(seconds, deadline - started)
    if seconds <= 0:  # el turno esperó al interruptor o a la reserva más de lo que quedaba
        settle(ctx, held, Outcome.PROVIDER_UNAVAILABLE, Decimal(0), None, started)
        raise GatewayError(Outcome.PROVIDER_UNAVAILABLE, held)
    try:
        reply = adapter.send(key, body, seconds)
    except ProviderError as error:
        if error.billed:  # salió y no hubo respuesta: cuenta entera
            settle(ctx, held, Outcome.PROVIDER_UNCERTAIN, None, None, started, error.request_id)
        else:  # no salió, o un error sin uso: se libera
            settle(ctx, held, error.outcome, Decimal(0), None, started, error.request_id)
        raise GatewayError(error.outcome, held) from None
    # F8-07d: una tool no ofrecida o un id ya usado en la conversación no se podrían reenviar
    offered = {tool.name for tool in request.tools}
    used = {b.id for t in request.turns if type(t.content) is tuple
            for b in t.content if type(b) is ToolCall}  # fmt: skip
    if any(call.name not in offered or call.id in used for call in reply.tool_calls):
        settle(ctx, held, Outcome.PROVIDER_UNCERTAIN, None, None, started, reply.request_id)
        raise GatewayError(Outcome.PROVIDER_UNAVAILABLE, held)
    cost = prices.cost(provider, model, reply.usage)
    if cost > 0:
        settle(ctx, held, Outcome.OK, cost, reply, started)
    else:  # una respuesta sin uso no se cobra a 0 (la base lo rechaza): cuenta entera
        settle(ctx, held, Outcome.PROVIDER_UNCERTAIN, None, None, started, reply.request_id)
    return replace(reply, reservation_id=held)
