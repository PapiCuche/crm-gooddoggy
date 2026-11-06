"""La ingesta de los webhooks de Meta (F7-03b; ADR-029 §3): guardar cada ítem firmado.

`webhook_ingress` y `webhook_failures` son de plataforma: `crm_app` no tiene ningún privilegio
sobre ellas y solo entra por las funciones `SECURITY DEFINER` de las migraciones (`0008`: insertar).
Aquí está todo lo que las llama. Reclamar, enrutar y cerrar los ítems es de F7-03c.
"""

import math
from typing import Any

from django.db import DataError, connection, transaction
from psycopg.types.json import Jsonb

from core.observability.logging import get_logger

logger = get_logger(__name__)
PROVIDER = "meta"


def clean(value: Any) -> Any:
    """El payload sin lo que `jsonb` no guarda y un cliente podría escribir: `\\u0000` (se sustituye
    por U+FFFD) y los sustitutos sueltos (por `?`). La profundidad ya está acotada (`MAX_DEPTH`)."""
    if isinstance(value, float) and not math.isfinite(value):
        return None  # `1e999` llega como infinito, y `jsonb` no guarda `Infinity`: un 503 eterno
    if isinstance(value, str):
        return value.replace("\x00", "\ufffd").encode("utf-8", "replace").decode("utf-8")
    if isinstance(value, list):
        return [clean(item) for item in value]
    if isinstance(value, dict):
        return {clean(key): clean(item) for key, item in value.items()}
    return value


def _call(sql: str, params: list[Any]) -> list[tuple[Any, ...]]:
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        rows: list[tuple[Any, ...]] = cursor.fetchall()
        return rows


def insert(kind: str, key: str, waba_id: str, number: str | None, payload: dict[str, Any]) -> bool:
    """Guarda un ítem validado, idempotente por `(proveedor, clave)`. Devuelve si era nuevo."""
    sql = "SELECT public.webhook_ingress_insert(%s, %s, %s, %s, %s, %s)"
    found = _call(sql, [PROVIDER, key, kind, waba_id, number, Jsonb(clean(payload))])
    return found[0][0] is not None


def store(items: list[Any]) -> int:
    """Guarda todos los ítems de una entrega en una transacción (un reintento de Meta repite lo
    que falte y nada más). Devuelve cuántos eran nuevos."""
    new = refused = 0
    with transaction.atomic():
        for i in items:
            try:
                with transaction.atomic():  # un punto de guardado por ítem
                    new += insert(i.kind, i.key, i.waba_id, i.phone_number_id, i.payload)
            except DataError:  # un dato que la base nunca guardará: solo ese ítem se descarta, y
                refused += 1  # no un 503 que Meta repetiría eternamente
    if refused:
        logger.warning("meta.webhook.refused", count=refused)
    return new
