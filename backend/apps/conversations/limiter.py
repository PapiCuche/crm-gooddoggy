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

# Cuenta y fija el plazo en una sola operación: sin una pausa entre las dos, un fallo no deja una
# clave eterna que bloquee la cuenta para siempre.
COUNT = "local n = redis.call('INCR', KEYS[1]) if n == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end return n"  # noqa: E501


@cache
def client() -> redis.Redis:
    return redis.Redis.from_url(
        settings.CELERY_BROKER_URL, socket_connect_timeout=2, socket_timeout=2
    )


def allow(account_id: UUID) -> bool:
    """¿Cabe otro envío de la cuenta en la ventana actual? Todo intento cuenta. Lanza si Redis no
    contesta: quien llama lo trata como un fallo pasajero (el mensaje no sale)."""
    limit, window = settings.CHANNEL_SEND_LIMIT
    return int(client().eval(COUNT, 1, f"chn:send:{account_id}", window)) <= limit
