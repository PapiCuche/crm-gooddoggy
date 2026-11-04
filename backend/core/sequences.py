"""Numeración comercial por organización (ADR-004 §2): `COT-000001`, `OPP-000001`…

Una sola sentencia atómica en la transacción del llamador: el `ON CONFLICT DO UPDATE`
bloquea la fila (organización, clave) hasta el commit, así que las transacciones concurrentes
de la misma organización y clave se serializan y un rollback no consume número. Los números
nunca autorizan nada: leerlos o buscarlos exige tenant y permisos como cualquier dato.
"""

import re

from django.db import connections

from core.tenancy.context import TenantContext, TenantContextError, current

KEY = re.compile(r"[A-Z][A-Z0-9]{0,15}")  # con fullmatch: sin "\n" final
_ALLOCATE = (
    "INSERT INTO org_sequences (organization_id, sequence_key, next_value) "
    "VALUES (app_current_tenant(), %s, 2) "
    "ON CONFLICT (organization_id, sequence_key) "
    "DO UPDATE SET next_value = org_sequences.next_value + 1 "
    "RETURNING next_value - 1"
)


def format_number(prefix: str, value: int, width: int = 6) -> str:
    """`COT-000001`; al superar el ancho crece (`COT-1000000`).

    Como texto no conserva el orden: se ordena por el valor numérico, no por la cadena.
    """
    return f"{prefix}-{value:0{width}d}"


def allocate(
    ctx: TenantContext,
    key: str,
    *,
    prefix: str | None = None,
    width: int = 6,
    using: str = "default",
) -> str:
    """Asigna el siguiente número de `key` en la organización de `ctx`.

    Falla fuera de `transaction.atomic()` o si `ctx` no es el tenant activo. Asignar lo más
    tarde posible dentro de la transacción y mantenerla corta (contención por clave).
    """
    if not KEY.fullmatch(key):
        raise ValueError(f"Clave de secuencia inválida: {key!r}")
    connection = connections[using]
    if not connection.in_atomic_block:
        raise TenantContextError("allocate() requiere transaction.atomic()")
    active = current()
    if active is None or active.organization_id != ctx.organization_id:
        raise TenantContextError("allocate() requiere el tenant_scope de la misma organización")
    with connection.cursor() as cursor:
        cursor.execute(_ALLOCATE, [key])
        (value,) = cursor.fetchone()
    return format_number(prefix or key, int(value), width)
