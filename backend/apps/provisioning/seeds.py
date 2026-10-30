"""Datos por defecto de una organización nueva (F5-01).

Los módulos de negocio (L3) están sobre `provisioning` y no pueden importarse desde él: cada uno
registra aquí, desde su `AppConfig.ready`, lo que su organización necesita para ser usable, y el
alta lo ejecuta en su transacción. Las organizaciones que ya existían lo reciben con una
migración de datos del propio módulo.
"""

from collections.abc import Callable

from core.tenancy.context import TenantContext

Seed = Callable[[TenantContext], object]
_SEEDS: list[Seed] = []


def register_seed(seed: Seed) -> None:
    """Idempotente: `ready` puede ejecutarse más de una vez en los tests."""
    if seed not in _SEEDS:
        _SEEDS.append(seed)


def run_seeds(ctx: TenantContext) -> None:
    """Dentro del `tenant_scope` de la organización nueva; cada semilla es idempotente."""
    for seed in _SEEDS:
        seed(ctx)
