"""Tareas de `ai_gateway` (F8-03a): el re-cifrado de las claves de proveedor tras rotar la clave
maestra (ADR-022 §5) y el barrido de reservas huérfanas (F8-05b5)."""

from apps.ai_gateway import keys, orphans
from core.tenancy.celery import platform_task


@platform_task(name="ai_gateway.reseal_provider_keys")
def reseal_provider_keys(max_orgs: int = 50, batch_size: int = 100) -> dict[str, int]:
    """Vuelve a cifrar con la clave vigente las claves de proveedor que quedaron con otra.
    Devuelve la clave vigente y cuántas volvió a cifrar, no pudo abrir o dejó para otra pasada."""
    return keys.reseal(max_orgs=max_orgs, batch_size=batch_size)


@platform_task(name="ai_gateway.close_orphan_reservations", ignore_result=True)
def close_orphan_reservations(max_orgs: int = 50, batch_size: int = 100) -> dict[str, int]:
    """Cierra como gastadas las reservas de IA huérfanas (ADR-034 §5): sin tenant al empezar; el
    de cada una sale de la base. Devuelve solo cuentas."""
    return orphans.close(max_orgs=max_orgs, batch_size=batch_size)
