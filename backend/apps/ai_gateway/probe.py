"""La prueba de conexión de una clave de proveedor (F8-03b; ADR-034 §3, §4): la llamada más barata
que la valida (listar modelos, sin tokens) al host fijo del proveedor, por `core.http` (https,
allowlist, IP pública fijada, tope de respuesta). Devuelve solo un código de un conjunto cerrado,
nunca el cuerpo del proveedor. La clave va en la cabecera `x-api-key`, nunca en la URL, y no llega
a un error ni a un registro: de la respuesta solo se registran el estado y el `request-id`.
"""

import json
from enum import StrEnum
from typing import Any

from django.conf import settings

from core import http
from core.observability.logging import get_logger

logger = get_logger(__name__)
API_VERSION = "2026-10-21"  # la cabecera `anthropic-version`
# El 400 de un límite de gasto que fijó el propietario en la consola (ADR-034 §3).
SPEND_LIMIT = "You have reached your specified"
SPEND_CODE = "enforced_spend_limit_reached"  # el 429 del tope de gasto del nivel de uso


class Probe(StrEnum):
    OK = "OK"
    CREDENTIALS_REJECTED = "CREDENTIALS_REJECTED"
    PROVIDER_BILLING = "PROVIDER_BILLING"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"


def _error(raw: bytes) -> dict[str, Any]:
    """El objeto `error` de una respuesta de Anthropic, o `{}` si no es lo que se espera."""
    try:
        error = json.loads(raw)["error"]
    except ValueError, KeyError, TypeError, RecursionError:
        return {}
    return error if isinstance(error, dict) else {}


def _classify(status: int, raw: bytes) -> Probe:
    error = _error(raw) if status != 200 else {}
    details = error.get("details")
    code = details.get("error_code") if isinstance(details, dict) else None
    message = error.get("message")
    if status == 200:
        try:
            listed = json.loads(raw)["data"]
        except ValueError, KeyError, TypeError, RecursionError:
            return Probe.PROVIDER_UNAVAILABLE  # una respuesta que no es del proveedor
        return Probe.OK if isinstance(listed, list) else Probe.PROVIDER_UNAVAILABLE
    if status in (401, 403):
        return Probe.CREDENTIALS_REJECTED
    if status == 402 or (status == 429 and code == SPEND_CODE):
        return Probe.PROVIDER_BILLING
    if (
        status == 400
        and error.get("type") == "invalid_request_error"
        and isinstance(message, str)
        and message.startswith(SPEND_LIMIT)
    ):
        return Probe.PROVIDER_BILLING
    return Probe.PROVIDER_UNAVAILABLE  # 429, 5xx, 529 y lo inesperado (queda en el log)


def check(provider: str, key: str) -> Probe:
    """Prueba `key` contra el proveedor: `OK` o el código de por qué no sirve. Sin reintentos ni
    esperas de `Retry-After` (`retry=False`): corre dentro de una transacción y, desde F8-03d, de
    una petición web; quien prueba puede volver a hacerlo."""
    url = f"https://{settings.AI_PROVIDER_HOSTS[provider]}/v1/models?limit=1"
    headers = {"x-api-key": key, "anthropic-version": API_VERSION}
    try:
        reply = http.request("GET", url, headers=headers, retry=False)
    except http.BlockedDestination, http.HTTPError, OSError:
        logger.warning("ai.provider_probe_failed", provider=provider)  # sin el mensaje
        return Probe.PROVIDER_UNAVAILABLE
    result = _classify(reply.status, reply.data)
    request_id = reply.headers.get("request-id", "")
    shown = request_id[:64] if request_id.isprintable() else ""
    logger.info(
        "ai.provider_probe", provider=provider, status=reply.status, result=result,
        request_id=shown,
    )  # fmt: skip
    return result
