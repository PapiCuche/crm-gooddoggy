"""Número de una oportunidad (`OPP-000123`, ADR-004 §2): la secuencia `OPP` de la organización.

`core.sequences.allocate` toma el número con una sola sentencia que bloquea la fila de la
secuencia hasta el commit: dos oportunidades a la vez no repiten número y un rollback no lo gasta.
Se llama dentro de la transacción que inserta la oportunidad y lo más tarde posible.
"""

from core.sequences import allocate
from core.tenancy.context import TenantContext
from core.tenancy.scope import require_scope

KEY = "OPP"


def next_opportunity_number(ctx: TenantContext) -> str:
    return allocate(ctx, KEY, using=require_scope(ctx))
