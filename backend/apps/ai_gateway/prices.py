"""El precio de una llamada (F8-05; ADR-034 §3, §5) con `AI_MODELS`: la cota que se reserva y lo
que costó. USD con `Decimal`, redondeado hacia arriba a 6 decimales."""

from decimal import ROUND_CEILING, Decimal
from typing import Any

from django.conf import settings

from apps.ai_gateway.llm import Usage

MILLION, STEP = Decimal(1_000_000), Decimal("0.000001")
INPUT = ("input", "cache_write_5m", "cache_write_1h", "cache_read")


class Unpriced(Exception):  # noqa: N818 — un estado de la configuración
    """El modelo no está en la lista permitida con su precio: no se llama."""


def _entry(provider: str, model: str) -> dict[str, Any]:
    try:
        entry: dict[str, Any] = settings.AI_MODELS[provider][model]
    except KeyError:
        raise Unpriced from None
    return entry


def _usd(amount: Decimal) -> Decimal:
    return (amount / MILLION).quantize(STEP, rounding=ROUND_CEILING)


def ceiling(provider: str, model: str, prompt_tokens: int, max_tokens: int) -> Decimal:
    """Lo más que puede costar: todo al precio de entrada más alto (caché y tramo largo incluidos)
    y `max_tokens` al de salida más alto. `Unpriced`."""
    entry = _entry(provider, model)
    tiers = [entry, entry["long"]] if "long" in entry else [entry]
    top_in = max(Decimal(tier[name]) for tier in tiers for name in INPUT)
    top_out = max(Decimal(tier["output"]) for tier in tiers)
    return _usd(prompt_tokens * top_in + max_tokens * top_out)


def cost(provider: str, model: str, usage: Usage) -> Decimal:
    """Lo que costó una llamada con el uso que informa el proveedor. `Unpriced`."""
    entry = _entry(provider, model)
    long = entry.get("long")
    counts = (usage.input_tokens, usage.cache_write_5m, usage.cache_write_1h, usage.cache_read)
    rate = long if long is not None and sum(counts) > long["above"] else entry
    total = sum(n * Decimal(rate[name]) for n, name in zip(counts, INPUT, strict=True))
    return _usd(total + usage.output_tokens * Decimal(rate["output"]))
