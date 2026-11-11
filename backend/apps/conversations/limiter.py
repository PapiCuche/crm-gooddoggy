"""El limitador de envíos por cuenta (F7-05c; ADR-029 §6): un contador de ventana fija en Redis (el
del broker de Celery), muy por debajo del tope de Meta, para que una ráfaga no agote el rendimiento
de un número ni lo ponga en riesgo. Sin cupo, el envío se devuelve a la cola y se reintenta; con
Redis caído **falla cerrado hacia el reintento**, nunca hacia enviar sin límite. La clave lleva solo
el id de la cuenta (no hay teléfonos ni textos) y la URL del broker nunca se registra.
"""

from functools import cache
from uuid import UUID

import redis
from django.conf import settings


@cache
def client() -> redis.Redis:
    return redis.Redis.from_url(
        settings.CELERY_BROKER_URL, socket_connect_timeout=2, socket_timeout=2
    )


def allow(account_id: UUID) -> bool:
    """¿Cabe otro envío de la cuenta en la ventana actual? Todo intento cuenta. Fija el plazo de la
    clave al crearla y **también si no lo tiene** (`TTL < 0`: una clave que perdió su `EXPIRE`
    bloquearía la cuenta para siempre). Lanza si Redis no contesta: quien llama lo trata como un
    fallo pasajero (el mensaje no sale)."""
    limit, window = settings.CHANNEL_SEND_LIMIT
    key, server = f"chn:send:{account_id}", client()
    with server.pipeline(transaction=True) as pipe:
        pipe.incr(key)
        pipe.ttl(key)
        count, ttl = pipe.execute()
    if count == 1 or ttl < 0:
        server.expire(key, window)
    return int(count) <= limit
