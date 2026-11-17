"""El único punto por el que pasa toda llamada a un modelo (F8-05; ADR-034 §2, §5): abre la clave
solo en memoria, **reserva** la cota superior del costo con una actualización atómica condicionada
(`gastado + reservado + estimado <= tope`) en su propia transacción, llama fuera de toda
transacción y **ajusta** una sola vez con el uso real en `ai_llm_calls` (nunca el prompt ni la
respuesta). Solo libera si la petición no salió o hubo una respuesta de error; lo dudoso, y una
reserva huérfana, cuentan enteros. Los permisos de negocio, en las rutas que lo usan (F8-08)."""

import time
from decimal import Decimal
from uuid import UUID

from django.conf import settings
from django.db import connections

from apps.ai_gateway import keys, prices
from apps.ai_gateway.anthropic import AnthropicAdapter
from apps.ai_gateway.llm import LLMProviderAdapter, LLMRequest, LLMResponse, Outcome, ProviderError
from apps.ai_gateway.models import AILLMCall
from core import sealed
from core.observability.logging import get_logger
from core.tenancy.context import TenantContext
from core.tenancy.scope import require_scope, tenant_scope

logger = get_logger(__name__)
ADAPTERS: dict[str, LLMProviderAdapter] = {"ANTHROPIC": AnthropicAdapter()}
# El mes en UTC (`now()` de la base): `organizations` no tiene zona horaria (decisión abierta).
OPEN = (
    "INSERT INTO ai_budget_usage (id, organization_id, month, spent_usd, reserved_usd) VALUES"
    " (uuidv7(), %s, date_trunc('month', now() AT TIME ZONE 'UTC')::date, 0, 0)"
    " ON CONFLICT (organization_id, month) DO NOTHING"
)
RESERVE = (
    "UPDATE ai_budget_usage u SET reserved_usd = u.reserved_usd + %(amount)s FROM ai_budgets b"
    " WHERE b.organization_id = u.organization_id AND u.organization_id = %(org)s"
    " AND u.month = date_trunc('month', now() AT TIME ZONE 'UTC')::date"
    " AND u.spent_usd + u.reserved_usd + %(amount)s <= b.monthly_usd RETURNING u.month"
)
SETTLE = (
    "UPDATE ai_llm_calls SET settled_at = now(), cost_usd = coalesce(%s, reserved_usd),"
    " outcome = %s, input_tokens = %s, output_tokens = %s, cache_write_tokens = %s,"
    " cache_read_tokens = %s, latency_ms = %s, request_id = %s"
    " WHERE id = %s AND settled_at IS NULL RETURNING month, reserved_usd, cost_usd"
)
SPEND = (
    "UPDATE ai_budget_usage SET reserved_usd = reserved_usd - %s, spent_usd = spent_usd + %s"
    " WHERE organization_id = %s AND month = %s"
)


class GatewayError(Exception):  # noqa: N818 — solo lleva el código
    def __init__(self, outcome: Outcome) -> None:
        super().__init__(outcome.value)
        self.outcome = outcome


def _reserve(ctx: TenantContext, request: LLMRequest, provider: str, amount: Decimal) -> UUID:
    alias, org = require_scope(ctx), ctx.organization_id
    with connections[alias].cursor() as cursor:
        cursor.execute(OPEN, [org])
        cursor.execute(RESERVE, {"amount": amount, "org": org})
        if (row := cursor.fetchone()) is None:
            raise GatewayError(Outcome.AI_BUDGET_EXHAUSTED)  # el scope deshace la fila del mes
    call = AILLMCall.objects.using(alias).create(
        organization_id=org, month=row[0], provider=provider, purpose=request.purpose,
        model=settings.AI_PROVIDER_PARAMS[provider]["model"], reserved_usd=amount,
        conversation_id=request.conversation_id, requested_by_user_id=ctx.user_id,
    )  # fmt: skip
    call_id: UUID = call.pk
    return call_id


def settle(
    ctx: TenantContext, call_id: UUID, outcome: Outcome, cost: Decimal | None,
    reply: LLMResponse | None = None, started: float | None = None,
) -> bool:  # fmt: skip
    """Ajusta la reserva una sola vez: `cost` (`None`: la reserva entera) pasa a gastado y el resto
    se libera. `False` si ya estaba ajustada (no cambia nada)."""
    use = reply.usage if reply else None
    tokens = (use.input_tokens, use.output_tokens, use.cache_write_5m + use.cache_write_1h,
              use.cache_read) if use else (None,) * 4  # fmt: skip
    lapse = int((time.monotonic() - started) * 1000) if started else None
    with tenant_scope(ctx), connections[require_scope(ctx)].cursor() as cursor:
        cursor.execute(SETTLE, [cost, outcome.value, *tokens, lapse,
                                reply.request_id if reply else "", call_id])  # fmt: skip
        if (row := cursor.fetchone()) is None:
            return False
        cursor.execute(SPEND, [row[1], row[2], ctx.organization_id, row[0]])
    logger.info("ai.llm_call", call_id=str(call_id), outcome=outcome)
    return True


def complete(ctx: TenantContext, request: LLMRequest, provider: str = "ANTHROPIC") -> LLMResponse:
    """Una llamada al modelo con el presupuesto de la organización de `ctx`. Se llama **fuera** de
    toda transacción (abre las suyas). `GatewayError` con el código si no hay respuesta."""
    if connections["default"].in_atomic_block:
        raise RuntimeError("complete() abre sus transacciones: ninguna puede estar abierta")
    params, adapter = settings.AI_PROVIDER_PARAMS[provider], ADAPTERS[provider]
    body = adapter.encode(request, params)
    try:  # un token nunca es menos de un byte
        amount = prices.ceiling(provider, params["model"], len(body) + settings.AI_TOOL_TOKENS,
                                params["max_tokens"])  # fmt: skip
    except prices.Unpriced:
        raise GatewayError(Outcome.AI_NOT_CONFIGURED) from None
    with tenant_scope(ctx):
        try:
            key = keys.open_key(ctx, provider)  # solo en memoria, para esta llamada
        except sealed.SealError:
            raise GatewayError(Outcome.AI_NOT_CONFIGURED) from None
        call_id = _reserve(ctx, request, provider, amount)
    started = time.monotonic()
    try:
        reply = adapter.send(key, body, float(params["seconds"]))
    except ProviderError as error:
        settle(ctx, call_id, error.outcome, None if error.billed else Decimal(0), None, started)
        raise GatewayError(error.outcome) from None
    cost = prices.cost(provider, params["model"], reply.usage)
    settle(ctx, call_id, Outcome.OK, cost, reply, started)
    return reply
