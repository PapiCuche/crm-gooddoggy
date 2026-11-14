"""Health checks (ADR-011 §4): sin versiones, hosts ni detalles de error. El motivo de un fallo va
solo al log, con el tipo de la excepción. `/health/*` es una sonda interna: el proxy no la publica
(infra/docker/proxy/Caddyfile) y el HEALTHCHECK de las imágenes usa `/health/live`."""

import logging
import threading
import time
from typing import Any

import redis
from django.conf import settings
from django.db import connection
from django.http import HttpRequest, JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe
from redis.backoff import NoBackoff
from redis.retry import Retry

from core import storage

logger = logging.getLogger(__name__)
TIMEOUT = 1.0  # segundos por comprobación, sin reintentos: menos que el intervalo de la sonda
READY_TTL = 5.0  # una ráfaga de sondas cuesta como mucho una comprobación por proceso y ventana
_last: dict[str, Any] = {}
_lock = threading.Lock()  # sondas a la vez en un proceso: una sola comprobación, las demás esperan


@never_cache
@require_safe
def live(request: HttpRequest) -> JsonResponse:
    """El proceso responde. No toca dependencias (liveness)."""
    return JsonResponse({"status": "ok"})


def _failed(what: str, exc: Exception) -> bool:
    logger.warning("readiness: %s no disponible (%s)", what, type(exc).__name__)
    return False


def _database_ok() -> bool:
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            return bool(cursor.fetchone() == (1,))
    except Exception as exc:  # noqa: BLE001 — cualquier fallo = no listo; el detalle solo va al log
        return _failed("base de datos", exc)


def _redis_ok() -> bool:
    """`PING` al broker y a la capa de canales (una vez si son la misma URL). Sin URL, no listo."""
    urls = sorted({settings.CELERY_BROKER_URL, settings.CHANNEL_LAYER_REDIS_URL} - {""})
    if not urls:
        return _failed("Redis", LookupError())
    for url in urls:
        try:
            client = redis.Redis.from_url(
                url, socket_connect_timeout=TIMEOUT, socket_timeout=TIMEOUT,
                retry=Retry(NoBackoff(), 0),
            )  # fmt: skip
            try:
                client.ping()
            finally:
                client.close()
        except Exception as exc:  # noqa: BLE001
            return _failed("Redis", exc)
    return True


def _storage_ok() -> bool:
    try:
        storage.ping(TIMEOUT)
    except Exception as exc:  # noqa: BLE001
        return _failed("almacenamiento", exc)
    return True


def forget() -> None:
    """Olvida el último resultado (tests)."""
    _last.clear()


def _checks() -> dict[str, str]:
    with _lock:
        if _last and time.monotonic() - _last["at"] < READY_TTL:
            return dict(_last["checks"])
        checks = {
            name: "ok" if check() else "fail"
            for name, check in (("database", _database_ok), ("redis", _redis_ok),
                                ("storage", _storage_ok))
        }  # fmt: skip
        _last.update(at=time.monotonic(), checks=checks)  # la ventana empieza al terminar
        return dict(checks)


@never_cache
@require_safe
def ready(request: HttpRequest) -> JsonResponse:
    """Dependencias listas (readiness): base de datos, Redis y almacenamiento."""
    checks = _checks()
    healthy = all(v == "ok" for v in checks.values())
    return JsonResponse(
        {"status": "ok" if healthy else "fail", "checks": checks}, status=200 if healthy else 503
    )
