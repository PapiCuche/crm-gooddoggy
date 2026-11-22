"""El texto de traspaso del copiloto que reconoce la solicitud de atención humana (F8-08l2, #1058).

`conversations` (L3) no importa `ai_agents` (L4): este lo sirve desde su `AppConfig.ready`, como
`provisioning.seeds` (el módulo de abajo expone el punto de enganche, el de arriba se engancha).
Un solo servidor; sin él, falla: nunca se compara con otro texto."""

from collections.abc import Callable

from django.core.exceptions import ImproperlyConfigured

from core.tenancy.context import TenantContext

Source = Callable[[TenantContext], str]
_SOURCE: list[Source] = []


def serve(source: Source) -> None:
    """Idempotente (`ready` puede correr más de una vez en los tests); otro servidor, no."""
    if _SOURCE and _SOURCE[0] is not source:
        raise ImproperlyConfigured("el texto de traspaso ya tiene quien lo sirva")
    _SOURCE[:] = [source]


def text(ctx: TenantContext) -> str:
    """El texto vigente de la organización de `ctx`, leído en su `tenant_scope` activo (RLS)."""
    if not _SOURCE:
        raise ImproperlyConfigured("nadie sirve el texto de traspaso")
    return _SOURCE[0](ctx)
